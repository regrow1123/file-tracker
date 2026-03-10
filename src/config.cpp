#include "config.h"
#include "toml.hpp"

#include <cstdio>

bool Config::load(const std::string& path) {
    try {
        auto tbl = toml::parse_file(path);

        // [kafka]
        if (auto kafka = tbl["kafka"].as_table()) {
            if (auto v = kafka->get("brokers"))
                kafka_brokers = v->value_or(kafka_brokers);
            if (auto v = kafka->get("topic"))
                kafka_topic = v->value_or(kafka_topic);
        }

        // [debounce]
        if (auto db = tbl["debounce"].as_table()) {
            if (auto v = db->get("quiet_ms"))
                debounce_quiet_ms = v->value_or(debounce_quiet_ms);
            if (auto v = db->get("max_wait_ms"))
                debounce_max_wait_ms = v->value_or(debounce_max_wait_ms);
        }

        // [wal]
        if (auto w = tbl["wal"].as_table()) {
            if (auto v = w->get("path"))
                wal_path = v->value_or(wal_path);
            if (auto v = w->get("max_size_mb"))
                wal_max_size_mb = v->value_or(wal_max_size_mb);
        }

        // [watch]
        if (auto w = tbl["watch"].as_table()) {
            if (auto v = w->get("prefix"))
                watch_prefix = v->value_or(watch_prefix);
        }

        // [logging]
        if (auto l = tbl["logging"].as_table()) {
            if (auto v = l->get("level"))
                log_level = v->value_or(log_level);
        }

        fprintf(stderr, "Config loaded from %s\n", path.c_str());
        return true;
    } catch (const toml::parse_error& err) {
        fprintf(stderr, "Config parse error: %s\n", err.what());
        return false;
    }
}
