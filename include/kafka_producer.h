#pragma once
#include <string>
#include <functional>
#include <librdkafka/rdkafka.h>

// Kafka producer wrapper.
// On send failure, calls the fallback callback (for WAL).
class KafkaProducer {
public:
    using FailCallback = std::function<void(const std::string& json)>;

    KafkaProducer(const std::string& brokers, const std::string& topic,
                  FailCallback on_fail);
    ~KafkaProducer();

    // Non-copyable
    KafkaProducer(const KafkaProducer&) = delete;
    KafkaProducer& operator=(const KafkaProducer&) = delete;

    // Send JSON message. Returns true if queued, false if failed (calls on_fail).
    bool send(const std::string& json, const std::string& key);

    // Poll for delivery reports. Call periodically.
    void poll(int timeout_ms = 0);

    // Flush pending messages (blocking).
    void flush(int timeout_ms = 5000);

    bool is_connected() const { return rk_ != nullptr; }

private:
    rd_kafka_t *rk_ = nullptr;
    rd_kafka_topic_t *rkt_ = nullptr;
    FailCallback on_fail_;

    static void dr_msg_cb(rd_kafka_t *rk, const rd_kafka_message_t *rkmessage, void *opaque);
};
