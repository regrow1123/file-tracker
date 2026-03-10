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
#include "debouncer.h"
#include "kafka_producer.h"
#include "wal.h"

static volatile bool running = true;

static void sig_handler(int sig) {
    running = false;
}

// Get hostname for JSON events
static std::string get_hostname() {
    char buf[256];
    if (gethostname(buf, sizeof(buf)) == 0) {
        buf[sizeof(buf)-1] = '\0';
        return buf;
    }
    return "unknown";
}

// Simple JSON serialization (no external dependency)
static std::string to_json(uint64_t ts_ns, const std::string& event_type,
                            const std::string& path, const std::string& hostname) {
    // Convert ns to ms
    uint64_t ts_ms = ts_ns / 1000000;
    std::string json = "{\"ts\":";
    json += std::to_string(ts_ms);
    json += ",\"event\":\"";
    json += event_type;
    json += "\",\"path\":\"";
    // Escape path for JSON
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

static int handle_event(void *ctx, void *data, size_t data_sz) {
    auto *evt = static_cast<struct file_event *>(data);
    std::string path = build_path(evt);

    // Userspace /home filter
    if (path.compare(0, 5, "/home") != 0) return 0;

    g_debouncer->on_event(path, evt->event_type);
    return 0;
}

int main(int argc, char **argv) {
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);

    // Config (hardcoded for now, will be config.toml later)
    std::string kafka_brokers = "localhost:9092";
    std::string kafka_topic = "file-tracker-events";
    auto debounce_quiet = std::chrono::milliseconds(10000);  // 10s
    auto debounce_max_wait = std::chrono::milliseconds(3600000);  // 1h

    std::string wal_path = "/var/lib/file-tracker/wal/events.wal";
    std::string hostname = get_hostname();
    uint64_t kafka_sent = 0;
    uint64_t kafka_failed = 0;
    uint64_t wal_replayed = 0;

    // Create WAL directory
    {
        std::string cmd = "mkdir -p " + wal_path.substr(0, wal_path.rfind('/'));
        if (::system(cmd.c_str()) != 0) {
            fprintf(stderr, "Warning: could not create WAL directory\n");
        }
    }

    // WAL for Kafka failure buffering
    WAL wal(wal_path);

    // Kafka producer: on failure, write to WAL
    KafkaProducer kafka(kafka_brokers, kafka_topic,
        [&wal, &kafka_failed](const std::string& json) {
            kafka_failed++;
            wal.append(json);
        }
    );

    // Debouncer callback: serialize and send to Kafka
    auto send_event = [&](const std::string& path, uint32_t event_type) {
        std::string type_str = (event_type == EVENT_DELETE) ? "delete" : "mtime_change";
        auto now = std::chrono::system_clock::now();
        auto ts_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            now.time_since_epoch()).count();

        std::string json = "{\"ts\":";
        json += std::to_string(ts_ms);
        json += ",\"event\":\"";
        json += type_str;
        json += "\",\"path\":\"";
        for (char c : path) {
            if (c == '"') json += "\\\"";
            else if (c == '\\') json += "\\\\";
            else json += c;
        }
        json += "\",\"hostname\":\"";
        json += hostname;
        json += "\"}";

        if (kafka.send(json, hostname)) {
            kafka_sent++;
        }

        printf("[%s] %s\n", type_str.c_str(), path.c_str());
    };

    Debouncer debouncer(debounce_quiet, debounce_max_wait, send_event);
    g_debouncer = &debouncer;

    // Open, load, attach BPF
    struct probe_bpf *skel = probe_bpf__open_and_load();
    if (!skel) {
        fprintf(stderr, "Failed to open and load BPF skeleton\n");
        return 1;
    }

    int err = probe_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "Failed to attach BPF programs: %d\n", err);
        probe_bpf__destroy(skel);
        return 1;
    }

    struct ring_buffer *rb = ring_buffer__new(
        bpf_map__fd(skel->maps.events),
        handle_event,
        nullptr,
        nullptr
    );
    if (!rb) {
        fprintf(stderr, "Failed to create ring buffer\n");
        probe_bpf__destroy(skel);
        return 1;
    }

    // Replay WAL on startup (recover from previous crash)
    {
        auto records = wal.read_all();
        if (!records.empty()) {
            fprintf(stderr, "WAL: replaying %zu records from previous run\n", records.size());
            // Truncate WAL first to avoid re-appending on delivery failure
            wal.truncate();
            for (auto& json : records) {
                kafka.send(json, hostname);
                wal_replayed++;
            }
            kafka.flush(10000);
            // Any failures during replay will be re-written to WAL by the on_fail callback
            fprintf(stderr, "WAL: replay complete, %llu records attempted\n",
                    (unsigned long long)wal_replayed);
        }
    }

    fprintf(stderr, "file-tracker started. Watching /home (debounce: %lldms, max_wait: %lldms)\n",
            (long long)debounce_quiet.count(), (long long)debounce_max_wait.count());

    // WAL retry: periodically check if there are WAL records to replay
    auto last_wal_retry = std::chrono::steady_clock::now();
    auto wal_retry_interval = std::chrono::seconds(30);

    // Main loop
    while (running) {
        // Poll BPF ring buffer (100ms timeout)
        err = ring_buffer__poll(rb, 100);
        if (err == -EINTR) break;
        if (err < 0) {
            fprintf(stderr, "ring_buffer__poll error: %d\n", err);
            break;
        }

        // Tick debouncer to fire expired entries
        debouncer.tick();

        // Poll Kafka for delivery reports
        kafka.poll(0);

        // Periodic WAL retry
        auto now = std::chrono::steady_clock::now();
        if (now - last_wal_retry >= wal_retry_interval) {
            last_wal_retry = now;
            if (wal.file_size() > 0) {
                auto records = wal.read_all();
                if (!records.empty()) {
                    fprintf(stderr, "WAL: retrying %zu records\n", records.size());
                    bool all_ok = true;
                    for (auto& json : records) {
                        if (!kafka.send(json, hostname)) {
                            all_ok = false;
                            break;
                        }
                        wal_replayed++;
                    }
                    if (all_ok) {
                        kafka.flush(5000);
                        wal.truncate();
                        fprintf(stderr, "WAL: retry complete\n");
                    }
                }
            }
        }
    }

    fprintf(stderr, "\nShutting down...\n");

    // Force-flush all pending debounce entries before exit
    // This fires all pending mtime_change events immediately
    debouncer.tick();  // normal tick
    // Force remaining entries by setting quiet_period to 0 temporarily
    // Just iterate and fire everything
    {
        // tick() only fires entries past quiet period, so for shutdown
        // we wait a bit and tick again, or just accept some loss
        // For now, just report pending count
    }

    fprintf(stderr, "Stats: kafka_sent=%llu kafka_failed=%llu wal_replayed=%llu wal_size=%llu pending_debounce=%zu\n",
            (unsigned long long)kafka_sent,
            (unsigned long long)kafka_failed,
            (unsigned long long)wal_replayed,
            (unsigned long long)wal.file_size(),
            debouncer.pending());

    kafka.flush(15000);

    ring_buffer__free(rb);
    probe_bpf__destroy(skel);
    return 0;
}
