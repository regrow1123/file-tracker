#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <string>
#include <chrono>
#include <thread>
#include <unistd.h>
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

// Build JSON event string
static std::string build_json(int64_t ts_ms, const char *event_type,
                               const std::string& path, const std::string& hostname) {
    std::string json = "{\"ts\":";
    json += std::to_string(ts_ms);
    json += ",\"event\":\"";
    json += event_type;
    json += "\",\"path\":\"";
    for (char c : path) {
        if (c == '"') json += "\\\"";
        else if (c == '\\') json += "\\\\";
        else json += c;
    }
    json += "\",\"hostname\":\"";
    json += hostname;
    json += "\"}";
    return json;
}

// Globals for ring buffer callback
static Debouncer *g_debouncer = nullptr;
static std::string g_watch_prefix;

static int handle_event(void *ctx, void *data, size_t data_sz) {
    auto *evt = static_cast<struct file_event *>(data);
    std::string path = build_path(evt);

    // Userspace prefix filter
    if (path.compare(0, g_watch_prefix.size(), g_watch_prefix) != 0) return 0;

    g_debouncer->on_event(path, evt->event_type);
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
    cfg.load(config_path);

    // Init logging
    Log::set_level_from_string(cfg.log_level.c_str());

    auto debounce_quiet = std::chrono::milliseconds(cfg.debounce_quiet_ms);
    auto debounce_max_wait = std::chrono::milliseconds(cfg.debounce_max_wait_ms);
    uint64_t wal_max_bytes = cfg.wal_max_size_mb * 1024ULL * 1024ULL;
    std::string hostname = get_hostname();
    uint64_t kafka_sent = 0;
    uint64_t kafka_failed = 0;
    uint64_t wal_replayed = 0;

    // Create WAL directory
    {
        std::string dir = cfg.wal_path.substr(0, cfg.wal_path.rfind('/'));
        std::string cmd = "mkdir -p " + dir;
        if (::system(cmd.c_str()) != 0) {
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

    // Debouncer callback
    auto send_event = [&](const std::string& path, uint32_t event_type) {
        const char *type_str;
        switch (event_type) {
            case EVENT_DELETE:      type_str = "delete"; break;
            case EVENT_MTIME:       type_str = "mtime_change"; break;
            case EVENT_RENAME_FROM: type_str = "rename_from"; break;
            case EVENT_RENAME_TO:   type_str = "rename_to"; break;
            default:                type_str = "unknown"; break;
        }
        auto ts_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();

        std::string json = build_json(ts_ms, type_str, path, hostname);

        if (kafka.send(json, hostname)) {
            kafka_sent++;
        }

        Log::debug("[%s] %s", type_str, path.c_str());
    };

    Debouncer debouncer(debounce_quiet, debounce_max_wait, send_event);
    g_debouncer = &debouncer;
    g_watch_prefix = cfg.watch_prefix;

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
        handle_event, nullptr, nullptr
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
            uint64_t new_drops = total_drops - prev_drops;
            prev_drops = total_drops;

            Log::info("Stats: bpf_events=%llu bpf_drops=%llu(+%llu) kafka_sent=%llu "
                      "kafka_failed=%llu wal_size=%llu pending=%zu",
                      (unsigned long long)total_events,
                      (unsigned long long)total_drops,
                      (unsigned long long)new_drops,
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
    uint64_t total_events = read_percpu_counter(counters_fd, 1);
    uint64_t total_drops = read_percpu_counter(counters_fd, 0);
    Log::info("Final stats: bpf_events=%llu bpf_drops=%llu kafka_sent=%llu "
              "kafka_failed=%llu wal_replayed=%llu wal_size=%llu",
              (unsigned long long)total_events,
              (unsigned long long)total_drops,
              (unsigned long long)kafka_sent,
              (unsigned long long)kafka_failed,
              (unsigned long long)wal_replayed,
              (unsigned long long)wal.file_size());

    kafka.flush(15000);
    ring_buffer__free(rb);
    probe_bpf__destroy(skel);
    return 0;
}
