#pragma once
#include <string>
#include <cstdint>

struct Config {
    // Kafka
    std::string kafka_brokers = "localhost:9092";
    std::string kafka_topic   = "file-tracker-events";

    // Debounce
    uint64_t debounce_quiet_ms    = 10000;    // 10s
    uint64_t debounce_max_wait_ms = 3600000;  // 1h

    // WAL
    std::string wal_path = "/var/lib/file-tracker/wal/events.wal";

    // Watch
    std::string watch_prefix = "/home";

    // Load from TOML file. Returns true on success.
    bool load(const std::string& path);
};
