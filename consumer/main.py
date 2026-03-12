#!/usr/bin/env python3
"""backup-consumer: Kafka 이벤트 → Redis 수집 데몬

단독 서비스로 실행. 백업은 backup-scheduler.service가 담당.
"""

import signal
import sys
import logging
from confluent_kafka import Consumer, KafkaError
from prometheus_client import start_http_server
import redis
import toml

from consumer import EventProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True
    )

    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)
    r.ping()
    log.info("Redis OK, pending: %d건", r.hlen("pending"))

    # Prometheus 메트릭 서버
    metrics_port = cfg.get("metrics", {}).get("port", 9101)
    start_http_server(metrics_port)
    log.info("Prometheus 메트릭: http://0.0.0.0:%d/metrics", metrics_port)

    kafka_consumer = Consumer({
        "bootstrap.servers": cfg["kafka"]["brokers"],
        "group.id": cfg["kafka"]["group_id"],
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
        "auto.commit.interval.ms": 5000,
    })
    kafka_consumer.subscribe([cfg["kafka"]["topic"]])
    log.info("Kafka consumer 시작: topic=%s", cfg["kafka"]["topic"])

    processor = EventProcessor(r)

    running = True
    def sig_handler(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    last_log_count = 0
    try:
        while running:
            msg = kafka_consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka 에러: %s", msg.error())
                continue

            processor.process(msg.value())

            if processor.stats["processed"] - last_log_count >= 1000:
                last_log_count = processor.stats["processed"]
                pending = r.hlen("pending")
                log.info("이벤트: %d건, 삭제스킵: %d, pending: %d",
                         processor.stats["processed"],
                         processor.stats["skipped_delete"], pending)
    finally:
        kafka_consumer.close()
        log.info("종료. 총 이벤트: %d건", processor.stats["processed"])


if __name__ == "__main__":
    main()
