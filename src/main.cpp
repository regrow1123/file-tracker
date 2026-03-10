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

    std::string hostname = get_hostname();
    uint64_t events_total = 0;
    uint64_t events_deduped = 0;
    uint64_t kafka_sent = 0;
    uint64_t kafka_failed = 0;

    // Kafka producer with WAL fallback (for now just log failures)
    KafkaProducer kafka(kafka_brokers, kafka_topic,
        [&kafka_failed](const std::string& json) {
            kafka_failed++;
            fprintf(stderr, "WAL: %s\n", json.c_str());
            // TODO: Phase 4 WAL implementation
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

    fprintf(stderr, "file-tracker started. Watching /home (debounce: %lldms, max_wait: %lldms)\n",
            (long long)debounce_quiet.count(), (long long)debounce_max_wait.count());

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
    }

    fprintf(stderr, "\nShutting down...\n");
    fprintf(stderr, "Stats: kafka_sent=%llu kafka_failed=%llu pending_debounce=%zu\n",
            (unsigned long long)kafka_sent,
            (unsigned long long)kafka_failed,
            debouncer.pending());

    // Flush remaining debounce entries
    // (in production, would save to WAL)
    kafka.flush(5000);

    ring_buffer__free(rb);
    probe_bpf__destroy(skel);
    return 0;
}
