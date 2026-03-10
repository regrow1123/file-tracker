#pragma once
#include <string>
#include <functional>
#include <unordered_map>
#include <chrono>
#include <mutex>

// Debouncer: for each path, fires callback only after quiet_period of no new events.
// If max_wait is exceeded since first event, fires immediately.
// Delete events bypass debounce entirely.
class Debouncer {
public:
    using Clock = std::chrono::steady_clock;
    using Callback = std::function<void(const std::string& path, uint32_t event_type)>;

    Debouncer(std::chrono::milliseconds quiet_period,
              std::chrono::milliseconds max_wait,
              Callback cb);

    // Called when a new event arrives
    void on_event(const std::string& path, uint32_t event_type);

    // Must be called periodically to fire expired timers
    void tick();

    // Flush all pending entries immediately (for graceful shutdown).
    // Returns number of entries flushed.
    size_t flush_all();

    // Number of pending entries
    size_t pending() const;

private:
    struct Entry {
        Clock::time_point first_seen;
        Clock::time_point last_seen;
    };

    std::chrono::milliseconds quiet_period_;
    std::chrono::milliseconds max_wait_;
    Callback cb_;
    mutable std::mutex mu_;
    std::unordered_map<std::string, Entry> entries_;
};
