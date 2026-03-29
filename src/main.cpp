#include <cstdio>
#include <cstring>
#include <csignal>
#include <string>
#include <functional>
#include <chrono>
#include <thread>
#include <unistd.h>
#include <sys/stat.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>

#include "probe.skel.h"
#include "common.h"
#include "config.h"
#include "log.h"
#include "debouncer.h"
#include "kafka_producer.h"
#include "wal.h"

static volatile bool running = true;

static void sig_handler(int sig) {
    running = false;
}

static std::string get_hostname() {
    char buf[256];
    if (gethostname(buf, sizeof(buf)) == 0) {
        buf[sizeof(buf)-1] = '\0';
        return buf;
    }
    return "unknown";
}

// Escape string for JSON (handles control characters)
static void json_escape(std::string& out, const std::string& s) {
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
}

// Build JSON event string
static std::string build_json(int64_t ts_ms, const char *event_type,
                               const std::string& path, const std::string& hostname) {
    std::string json = "{\"ts\":";
    json += std::to_string(ts_ms);
    json += ",\"event\":\"";
    json += event_type;
    json += "\",\"path\":\"";
    json_escape(json, path);
    json += "\",\"hostname\":\"";
    json_escape(json, hostname);
    json += "\"}";
    return json;
}

// Event handler context — passed to ring buffer callback via ctx pointer
struct EventCtx {
    Debouncer *debouncer = nullptr;
    std::string watch_prefix;
    std::function<void(const std::string&, const std::string&)> on_rename;

    // Rename pairing state
    std::string pending_rename_from;
    bool has_pending_rename = false;
};

static int handle_event(void *ctx, void *data, size_t data_sz) {
    auto *ectx = static_cast<EventCtx*>(ctx);
    auto *evt = static_cast<struct file_event *>(data);
    std::string path = build_path(evt);

    if (evt->event_type == EVENT_RENAME_FROM) {
        ectx->pending_rename_from = path;
        ectx->has_pending_rename = true;
        return 0;
    }

    if (evt->event_type == EVENT_RENAME_TO) {
        if (ectx->has_pending_rename) {
            bool old_match = ectx->pending_rename_from.compare(
                0, ectx->watch_prefix.size(), ectx->watch_prefix) == 0;
            bool new_match = path.compare(
                0, ectx->watch_prefix.size(), ectx->watch_prefix) == 0;

            if (old_match || new_match) {
                ectx->on_rename(ectx->pending_rename_from, path);
            }
            ectx->has_pending_rename = false;
        }
        return 0;
    }

    // Flush any orphaned rename_from (shouldn't happen normally)
    if (ectx->has_pending_rename) {
        ectx->has_pending_rename = false;
    }

    // Userspace prefix filter for delete/mtime events
    if (path.compare(0, ectx->watch_prefix.size(), ectx->watch_prefix) != 0) return 0;

    ectx->debouncer->on_event(path, evt->event_type);
    return 0;
}

// Read per-CPU counter from BPF map, sum across all CPUs
static uint64_t read_percpu_counter(int map_fd, uint32_t key) {
    int ncpus = libbpf_num_possible_cpus();
    if (ncpus <= 0) return 0;
    std::vector<uint64_t> values(ncpus, 0);
    if (bpf_map_lookup_elem(map_fd, &key, values.data()) != 0) return 0;
    uint64_t total = 0;
    for (int i = 0; i < ncpus; i++) total += values[i];
    return total;
}

int main(int argc, char **argv) {
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    // Config
    Config cfg;
    std::string config_path = "/etc/file-tracker/config.toml";
    if (argc > 1) config_path = argv[1];
    if (!cfg.load(config_path)) {
        fprintf(stderr, "Failed to load config: %s\n", config_path.c_str());
        return 1;
    }

    // Init logging
    Log::set_level_from_string(cfg.log_level.c_str());

    auto debounce_quiet = std::chrono::milliseconds(cfg.debounce_quiet_ms);
    auto debounce_max_wait = std::chrono::milliseconds(cfg.debounce_max_wait_ms);
    uint64_t wal_max_bytes = cfg.wal_max_size_mb * 1024ULL * 1024ULL;
    std::string hostname = get_hostname();
    uint64_t kafka_sent = 0;
    uint64_t kafka_failed = 0;
    uint64_t wal_replayed = 0;

    // Create WAL directory (recursive mkdir)
    {
        std::string dir = cfg.wal_path.substr(0, cfg.wal_path.rfind('/'));
        std::string acc;
        for (size_t i = 0; i < dir.size(); i++) {
            acc += dir[i];
            if (dir[i] == '/' || i == dir.size() - 1) {
                ::mkdir(acc.c_str(), 0755);
            }
        }
        struct stat st;
        if (stat(dir.c_str(), &st) != 0) {
            Log::warn("Could not create WAL directory: %s", dir.c_str());
        }
    }

    WAL wal(cfg.wal_path);

    // Kafka producer: on failure, write to WAL (with size limit)
    KafkaProducer kafka(cfg.kafka_brokers, cfg.kafka_topic,
        [&wal, &kafka_failed, wal_max_bytes](const std::string& json) {
            kafka_failed++;
            if (wal.file_size() < wal_max_bytes) {
                wal.append(json);
            } else {
                Log::error("WAL size limit reached (%llu bytes), dropping event",
                           (unsigned long long)wal_max_bytes);
            }
        }
    );

    // Debouncer callback (delete + mtime_change only)
    auto send_event = [&](const std::string& path, uint32_t event_type) {
        const char *type_str = (event_type == EVENT_DELETE) ? "delete" : "mtime_change";
        auto ts_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();

        std::string json = build_json(ts_ms, type_str, path, hostname);

        if (kafka.send(json, hostname)) {
            kafka_sent++;
        }

        Log::debug("[%s] %s", type_str, path.c_str());
    };

    Debouncer debouncer(debounce_quiet, debounce_max_wait, send_event);

    EventCtx ectx;
    ectx.debouncer = &debouncer;
    ectx.watch_prefix = cfg.watch_prefix;
    ectx.on_rename = [&](const std::string& old_path, const std::string& new_path) {
        auto ts_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();

        std::string json = "{\"ts\":";
        json += std::to_string(ts_ms);
        json += ",\"event\":\"rename\",\"old_path\":\"";
        json_escape(json, old_path);
        json += "\",\"new_path\":\"";
        json_escape(json, new_path);
        json += "\",\"hostname\":\"";
        json_escape(json, hostname);
        json += "\"}";

        if (kafka.send(json, hostname)) {
            kafka_sent++;
        }

        Log::debug("[rename] %s -> %s", old_path.c_str(), new_path.c_str());
    };

    // Open, load, attach BPF
    struct probe_bpf *skel = probe_bpf__open_and_load();
    if (!skel) {
        Log::error("Failed to open and load BPF skeleton");
        return 1;
    }

    int err = probe_bpf__attach(skel);
    if (err) {
        Log::error("Failed to attach BPF programs: %d", err);
        probe_bpf__destroy(skel);
        return 1;
    }

    struct ring_buffer *rb = ring_buffer__new(
        bpf_map__fd(skel->maps.events),
        handle_event, &ectx, nullptr
    );
    if (!rb) {
        Log::error("Failed to create ring buffer");
        probe_bpf__destroy(skel);
        return 1;
    }

    int counters_fd = bpf_map__fd(skel->maps.counters);

    // Replay WAL on startup
    {
        auto records = wal.read_all();
        if (!records.empty()) {
            Log::info("WAL: replaying %zu records from previous run", records.size());
            wal.truncate();
            for (auto& json : records) {
                kafka.send(json, hostname);
                wal_replayed++;
            }
            kafka.flush(10000);
            Log::info("WAL: replay complete, %llu records attempted",
                      (unsigned long long)wal_replayed);
        }
    }

    Log::info("file-tracker started (debounce: %lldms, max_wait: %lldms, watch: %s)",
              (long long)debounce_quiet.count(),
              (long long)debounce_max_wait.count(),
              cfg.watch_prefix.c_str());

    // Timers
    auto last_wal_retry = std::chrono::steady_clock::now();
    auto last_stats = std::chrono::steady_clock::now();
    auto wal_retry_interval = std::chrono::seconds(30);
    auto stats_interval = std::chrono::seconds(60);
    uint64_t prev_drops = 0;

    // Main loop
    while (running) {
        err = ring_buffer__poll(rb, 100);
        if (err == -EINTR) break;
        if (err < 0) {
            Log::error("ring_buffer__poll error: %d", err);
            break;
        }

        debouncer.tick();
        kafka.poll(0);

        auto now = std::chrono::steady_clock::now();

        // Periodic stats (every 60s)
        if (now - last_stats >= stats_interval) {
            last_stats = now;
            uint64_t total_events = read_percpu_counter(counters_fd, 1);
            uint64_t total_drops = read_percpu_counter(counters_fd, 0);
            uint64_t total_dedup = read_percpu_counter(counters_fd, 2);
            uint64_t new_drops = total_drops - prev_drops;
            prev_drops = total_drops;

            Log::info("Stats: bpf_events=%llu bpf_drops=%llu(+%llu) bpf_dedup=%llu "
                      "kafka_sent=%llu kafka_failed=%llu wal_size=%llu pending=%zu",
                      (unsigned long long)total_events,
                      (unsigned long long)total_drops,
                      (unsigned long long)new_drops,
                      (unsigned long long)total_dedup,
                      (unsigned long long)kafka_sent,
                      (unsigned long long)kafka_failed,
                      (unsigned long long)wal.file_size(),
                      debouncer.pending());

            if (new_drops > 0) {
                Log::warn("Ring buffer dropped %llu events in last interval!",
                          (unsigned long long)new_drops);
            }
        }

        // Periodic WAL retry
        if (now - last_wal_retry >= wal_retry_interval) {
            last_wal_retry = now;
            if (wal.file_size() > 0) {
                auto records = wal.read_all();
                if (!records.empty()) {
                    Log::info("WAL: retrying %zu records", records.size());
                    wal.truncate();
                    for (auto& json : records) {
                        kafka.send(json, hostname);
                        wal_replayed++;
                    }
                    kafka.flush(5000);
                    Log::info("WAL: retry complete");
                }
            }
        }
    }

    // Graceful shutdown
    Log::info("Shutting down...");

    // Flush all pending debounce entries
    size_t flushed = debouncer.flush_all();
    Log::info("Flushed %zu debounce entries", flushed);

    // Final stats
    {
        uint64_t total_events = read_percpu_counter(counters_fd, 1);
        uint64_t total_drops = read_percpu_counter(counters_fd, 0);
        uint64_t total_dedup = read_percpu_counter(counters_fd, 2);
        Log::info("Final stats: bpf_events=%llu bpf_drops=%llu bpf_dedup=%llu "
                  "kafka_sent=%llu kafka_failed=%llu wal_replayed=%llu wal_size=%llu",
                  (unsigned long long)total_events,
                  (unsigned long long)total_drops,
                  (unsigned long long)total_dedup,
                  (unsigned long long)kafka_sent,
                  (unsigned long long)kafka_failed,
                  (unsigned long long)wal_replayed,
                  (unsigned long long)wal.file_size());
    }

    kafka.flush(15000);
    ring_buffer__free(rb);
    probe_bpf__destroy(skel);
    return 0;
}
