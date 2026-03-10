#include "debouncer.h"
#include "common.h"

Debouncer::Debouncer(std::chrono::milliseconds quiet_period,
                     std::chrono::milliseconds max_wait,
                     Callback cb)
    : quiet_period_(quiet_period), max_wait_(max_wait), cb_(std::move(cb)) {}

void Debouncer::on_event(const std::string& path, uint32_t event_type) {
    // Delete and rename events bypass debounce entirely
    if (event_type == EVENT_DELETE ||
        event_type == EVENT_RENAME_FROM ||
        event_type == EVENT_RENAME_TO) {
        {
            std::lock_guard<std::mutex> lock(mu_);
            entries_.erase(path);
        }
        cb_(path, event_type);
        return;
    }

    // mtime_change → debounce
    bool fire_max_wait = false;
    {
        std::lock_guard<std::mutex> lock(mu_);
        auto now = Clock::now();
        auto it = entries_.find(path);
        if (it != entries_.end()) {
            if (now - it->second.first_seen >= max_wait_) {
                entries_.erase(it);
                fire_max_wait = true;
            } else {
                it->second.last_seen = now;
            }
        } else {
            entries_[path] = {now, now};
        }
    }

    if (fire_max_wait) {
        cb_(path, event_type);
        // Re-insert with fresh first_seen
        std::lock_guard<std::mutex> lock(mu_);
        auto now = Clock::now();
        entries_[path] = {now, now};
    }
}

void Debouncer::tick() {
    std::vector<std::pair<std::string, uint32_t>> to_fire;

    {
        std::lock_guard<std::mutex> lock(mu_);
        auto now = Clock::now();
        for (auto it = entries_.begin(); it != entries_.end(); ) {
            if (now - it->second.last_seen >= quiet_period_) {
                to_fire.emplace_back(it->first, EVENT_MTIME);
                it = entries_.erase(it);
            } else {
                ++it;
            }
        }
    }

    for (auto& [path, evt] : to_fire) {
        cb_(path, evt);
    }
}

size_t Debouncer::flush_all() {
    std::vector<std::pair<std::string, uint32_t>> to_fire;

    {
        std::lock_guard<std::mutex> lock(mu_);
        for (auto& [path, entry] : entries_) {
            to_fire.emplace_back(path, EVENT_MTIME);
        }
        entries_.clear();
    }

    for (auto& [path, evt] : to_fire) {
        cb_(path, evt);
    }
    return to_fire.size();
}

size_t Debouncer::pending() const {
    std::lock_guard<std::mutex> lock(mu_);
    return entries_.size();
}
