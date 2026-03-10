#include "kafka_producer.h"
#include <cstdio>
#include <cstring>

KafkaProducer::KafkaProducer(const std::string& brokers, const std::string& topic,
                             FailCallback on_fail)
    : on_fail_(std::move(on_fail))
{
    char errstr[512];
    rd_kafka_conf_t *conf = rd_kafka_conf_new();

    if (rd_kafka_conf_set(conf, "bootstrap.servers", brokers.c_str(),
                          errstr, sizeof(errstr)) != RD_KAFKA_CONF_OK) {
        fprintf(stderr, "kafka conf error: %s\n", errstr);
        rd_kafka_conf_destroy(conf);
        return;
    }

    // Compression
    rd_kafka_conf_set(conf, "compression.type", "lz4", errstr, sizeof(errstr));

    // Batch settings
    rd_kafka_conf_set(conf, "batch.num.messages", "1000", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "linger.ms", "100", errstr, sizeof(errstr));

    // Shorter timeout for faster failure detection when broker is down
    rd_kafka_conf_set(conf, "message.timeout.ms", "10000", errstr, sizeof(errstr));
    rd_kafka_conf_set(conf, "socket.timeout.ms", "5000", errstr, sizeof(errstr));

    // Delivery report callback
    rd_kafka_conf_set_dr_msg_cb(conf, dr_msg_cb);
    rd_kafka_conf_set_opaque(conf, this);

    rk_ = rd_kafka_new(RD_KAFKA_PRODUCER, conf, errstr, sizeof(errstr));
    if (!rk_) {
        fprintf(stderr, "kafka producer create failed: %s\n", errstr);
        // conf is destroyed by rd_kafka_new on failure
        return;
    }
    // conf is owned by rk_ now

    rkt_ = rd_kafka_topic_new(rk_, topic.c_str(), nullptr);
    if (!rkt_) {
        fprintf(stderr, "kafka topic create failed: %s\n", rd_kafka_err2str(rd_kafka_last_error()));
        rd_kafka_destroy(rk_);
        rk_ = nullptr;
        return;
    }

    fprintf(stderr, "Kafka producer connected to %s, topic: %s\n", brokers.c_str(), topic.c_str());
}

KafkaProducer::~KafkaProducer() {
    if (rk_) {
        // Longer flush to allow delivery callbacks to fire
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

    // Make a copy for rdkafka to own
    int err = rd_kafka_produce(
        rkt_,
        RD_KAFKA_PARTITION_UA,
        RD_KAFKA_MSG_F_COPY,
        const_cast<char*>(json.data()), json.size(),
        key.data(), key.size(),
        // opaque: store json copy for delivery failure callback
        new std::string(json)
    );

    if (err == -1) {
        fprintf(stderr, "kafka produce failed: %s\n",
                rd_kafka_err2str(rd_kafka_last_error()));
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
        fprintf(stderr, "kafka delivery failed: %s\n", rd_kafka_err2str(rkmessage->err));
        if (json_copy && self->on_fail_) {
            self->on_fail_(*json_copy);
        }
    }

    delete json_copy;
}
