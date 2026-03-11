#!/usr/bin/env python3
"""backup-consumer: Kafka → Redis 변경 DB 수집기"""

import json
import signal
import sys
import logging
from confluent_kafka import Consumer, KafkaError
import redis
import toml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("consumer")


class EventProcessor:
    """Kafka 이벤트를 Redis pending Hash에 upsert."""

    def __init__(self, redis_client: redis.Redis):
        self.r = redis_client
        self.stats = {"processed": 0, "skipped_old": 0, "errors": 0}

    def process(self, msg_value: bytes):
        try:
            event = json.loads(msg_value)
        except json.JSONDecodeError as e:
            log.warning("JSON 파싱 실패: %s", e)
            self.stats["errors"] += 1
            return

        event_type = event.get("event")
        ts = event.get("ts", 0)

        if event_type == "rename":
            self._handle_rename(event, ts)
        elif event_type in ("mtime_change", "delete"):
            path = event.get("path")
            if path:
                self._upsert(path, event_type, ts)
        else:
            log.warning("알 수 없는 이벤트: %s", event_type)
            self.stats["errors"] += 1
            return

        self.stats["processed"] += 1

    def _handle_rename(self, event: dict, ts: int):
        old_path = event.get("old_path")
        new_path = event.get("new_path")
        if not old_path or not new_path:
            log.warning("rename 이벤트에 old_path/new_path 없음: %s", event)
            self.stats["errors"] += 1
            return
        # old_path 제거
        self.r.hdel("pending", old_path)
        # new_path upsert
        info = json.dumps({"event": "rename", "old_path": old_path, "ts": ts})
        self.r.hset("pending", new_path, info)

    def _upsert(self, path: str, event_type: str, ts: int):
        existing = self.r.hget("pending", path)
        if existing:
            try:
                old_ts = json.loads(existing)["ts"]
                if ts <= old_ts:
                    self.stats["skipped_old"] += 1
                    return
            except (json.JSONDecodeError, KeyError):
                pass  # 기존 데이터 손상 → 덮어쓰기

        info = json.dumps({"event": event_type, "ts": ts})
        self.r.hset("pending", path, info)


def main():
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = "config.toml"

    cfg = toml.load(config_path)

    # Redis 연결
    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)
    r.ping()
    log.info("Redis 연결 OK")

    # Kafka consumer
    consumer = Consumer({
        "bootstrap.servers": cfg["kafka"]["brokers"],
        "group.id": cfg["kafka"]["group_id"],
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
        "auto.commit.interval.ms": 5000,
    })
    consumer.subscribe([cfg["kafka"]["topic"]])
    log.info("Kafka consumer 시작: topic=%s group=%s",
             cfg["kafka"]["topic"], cfg["kafka"]["group_id"])

    processor = EventProcessor(r)

    # graceful shutdown
    running = True
    def sig_handler(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    last_log_count = 0
    try:
        while running:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka 에러: %s", msg.error())
                continue

            processor.process(msg.value())

            # 100건마다 로그
            if processor.stats["processed"] - last_log_count >= 100:
                last_log_count = processor.stats["processed"]
                pending = r.hlen("pending")
                log.info("처리: %d건, 스킵(오래됨): %d, 에러: %d, pending: %d",
                         processor.stats["processed"],
                         processor.stats["skipped_old"],
                         processor.stats["errors"],
                         pending)
    finally:
        consumer.close()
        pending = r.hlen("pending")
        log.info("종료. 총 처리: %d건, 스킵: %d, 에러: %d, pending: %d",
                 processor.stats["processed"],
                 processor.stats["skipped_old"],
                 processor.stats["errors"],
                 pending)


if __name__ == "__main__":
    main()
