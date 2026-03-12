#!/usr/bin/env python3
"""backup-consumer: Kafka → Redis 변경 DB 수집기"""

import json
import signal
import sys
import logging
from confluent_kafka import Consumer, KafkaError
from prometheus_client import Counter, Gauge
import redis
import toml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("consumer")

# Prometheus 메트릭
EVENTS_PROCESSED = Counter(
    "backup_events_processed_total",
    "Total Kafka events processed")
EVENTS_SKIPPED_DELETE = Counter(
    "backup_events_skipped_delete_total",
    "Delete events skipped (not stored in pending)")
EVENTS_ERRORS = Counter(
    "backup_events_errors_total",
    "Events that failed to process")
PENDING_TOTAL = Gauge(
    "backup_pending_total",
    "Current number of files in Redis pending")
BACKUP_LAST_TIMESTAMP = Gauge(
    "backup_last_run_timestamp",
    "Unix timestamp of last backup run")
BACKUP_LAST_BACKED_UP = Gauge(
    "backup_last_run_backed_up",
    "Files backed up in last run")
BACKUP_LAST_ERRORS = Gauge(
    "backup_last_run_errors",
    "Errors in last backup run")
BACKUP_LAST_DURATION = Gauge(
    "backup_last_run_duration_seconds",
    "Duration of last backup run in seconds")


class EventProcessor:
    """Kafka 이벤트를 Redis pending Hash에 upsert.

    - delete 이벤트는 무시 (백업할 대상 없음, 이전 스냅샷에 보존)
    - ts 비교 없이 단순 HSET (Lustre 현재 상태를 직접 읽으므로 순서 무관)
    - RENAME 방식으로 backup과 분리되므로 레이스 없음
    """

    def __init__(self, redis_client: redis.Redis):
        self.r = redis_client
        self.stats = {"processed": 0, "skipped_delete": 0, "errors": 0}

    def process(self, msg_value: bytes):
        try:
            event = json.loads(msg_value)
        except json.JSONDecodeError as e:
            log.warning("JSON 파싱 실패: %s", e)
            self.stats["errors"] += 1
            EVENTS_ERRORS.inc()
            return

        event_type = event.get("event")

        if event_type == "delete":
            self.stats["skipped_delete"] += 1
            self.stats["processed"] += 1
            EVENTS_SKIPPED_DELETE.inc()
            EVENTS_PROCESSED.inc()
            return

        if event_type == "rename":
            self._handle_rename(event)
        elif event_type == "mtime_change":
            path = event.get("path")
            if path:
                self.r.hset("pending", path, event_type)
        else:
            log.warning("알 수 없는 이벤트: %s", event_type)
            self.stats["errors"] += 1
            EVENTS_ERRORS.inc()
            return

        self.stats["processed"] += 1
        EVENTS_PROCESSED.inc()

    def _handle_rename(self, event: dict):
        old_path = event.get("old_path")
        new_path = event.get("new_path")
        if not old_path or not new_path:
            log.warning("rename 이벤트에 old_path/new_path 없음: %s", event)
            self.stats["errors"] += 1
            EVENTS_ERRORS.inc()
            return
        pipe = self.r.pipeline()
        pipe.hdel("pending", old_path)
        pipe.hset("pending", new_path, "rename")
        pipe.execute()

    def update_metrics(self):
        """Prometheus gauge 갱신: pending 건수 + backup 실행 결과."""
        PENDING_TOTAL.set(self.r.hlen("pending"))
        last_run = self.r.hgetall("backup:last_run")
        if last_run:
            BACKUP_LAST_TIMESTAMP.set(float(last_run.get("timestamp", 0)))
            BACKUP_LAST_BACKED_UP.set(float(last_run.get("backed_up", 0)))
            BACKUP_LAST_ERRORS.set(float(last_run.get("errors", 0)))
            BACKUP_LAST_DURATION.set(float(last_run.get("duration", 0)))


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
    gauge_interval = 0
    try:
        while running:
            msg = consumer.poll(1.0)
            if msg is None:
                gauge_interval += 1
                if gauge_interval >= 10:  # 10초마다 gauge 갱신
                    processor.update_metrics()
                    gauge_interval = 0
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka 에러: %s", msg.error())
                continue

            processor.process(msg.value())
            gauge_interval = 0

            # 100건마다 로그
            if processor.stats["processed"] - last_log_count >= 100:
                last_log_count = processor.stats["processed"]
                processor.update_metrics()
                pending = r.hlen("pending")
                log.info("처리: %d건, 삭제스킵: %d, 에러: %d, pending: %d",
                         processor.stats["processed"],
                         processor.stats["skipped_delete"],
                         processor.stats["errors"],
                         pending)
    finally:
        consumer.close()
        pending = r.hlen("pending")
        log.info("종료. 총 처리: %d건, 삭제스킵: %d, 에러: %d, pending: %d",
                 processor.stats["processed"],
                 processor.stats["skipped_delete"],
                 processor.stats["errors"],
                 pending)


if __name__ == "__main__":
    main()
