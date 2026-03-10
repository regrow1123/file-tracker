#include "kafka_producer.h"
#include "log.h"
#include <cstring>

KafkaProducer::KafkaProducer(const std::string& brokers, const std::string& topic,
                             FailCallback on_fail)
    : on_fail_(std::move(on_fail))
{
    char errstr[512];
    rd_kafka_conf_t *conf = rd_kafka_conf_new();

    if (rd_kafka_conf_set(conf, "bootstrap.servers", brokers.c_str(),
                          errstr, sizeof(errstr)) != RD_KAFKA_CONF_OK) {
        Log::error("kafka conf error: %s", errstr);
        rd_kafka_conf_destroy(conf);
        return;
    }

    rd_kafka_conf_set(conf, "compression.type", "lz4", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "batch.num.messages", "1000", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "linger.ms", "100", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "message.timeout.ms", "10000", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "socket.timeout.ms", "5000", errstr, sizeof(errstr));

    // Suppress rdkafka's own logging (we handle errors via delivery callback)
    rd_kafka_conf_set(conf, "log_level", "0", errstr, sizeof(errstr));  // suppress all

    rd_kafka_conf_set_dr_msg_cb(conf, dr_msg_cb);
    rd_kafka_conf_set_opaque(conf, this);

    rk_ = rd_kafka_new(RD_KAFKA_PRODUCER, conf, errstr, sizeof(errstr));
    if (!rk_) {
        Log::error("kafka producer create failed: %s", errstr);
        return;
    }

    // Suppress internal rdkafka logs completely
    rd_kafka_set_log_level(rk_, 0);

    rkt_ = rd_kafka_topic_new(rk_, topic.c_str(), nullptr);
    if (!rkt_) {
        Log::error("kafka topic create failed: %s", rd_kafka_err2str(rd_kafka_last_error()));
        rd_kafka_destroy(rk_);
        rk_ = nullptr;
        return;
    }

    Log::info("Kafka producer connected to %s, topic: %s", brokers.c_str(), topic.c_str());
}

KafkaProducer::~KafkaProducer() {
    if (rk_) {
        rd_kafka_flush(rk_, 15000);
        if (rkt_) rd_kafka_topic_destroy(rkt_);
        rd_kafka_destroy(rk_);
    }
}

bool KafkaProducer::send(const std::string& json, const std::string& key) {
    if (!rk_ || !rkt_) {
        on_fail_(json);
        return false;
    }

    int err = rd_kafka_produce(
        rkt_, RD_KAFKA_PARTITION_UA, RD_KAFKA_MSG_F_COPY,
        const_cast<char*>(json.data()), json.size(),
        key.data(), key.size(),
        new std::string(json)
    );

    if (err == -1) {
        Log::error("kafka produce queue failed: %s", rd_kafka_err2str(rd_kafka_last_error()));
        on_fail_(json);
        return false;
    }

    return true;
}

void KafkaProducer::poll(int timeout_ms) {
    if (rk_) rd_kafka_poll(rk_, timeout_ms);
}

void KafkaProducer::flush(int timeout_ms) {
    if (rk_) rd_kafka_flush(rk_, timeout_ms);
}

void KafkaProducer::dr_msg_cb(rd_kafka_t *rk, const rd_kafka_message_t *rkmessage, void *opaque) {
    auto *self = static_cast<KafkaProducer*>(opaque);
    auto *json_copy = static_cast<std::string*>(rkmessage->_private);

    if (rkmessage->err) {
        Log::warn("kafka delivery failed: %s", rd_kafka_err2str(rkmessage->err));
        if (json_copy && self->on_fail_) {
            self->on_fail_(*json_copy);
        }
    }

    delete json_copy;
}
