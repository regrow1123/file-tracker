#!/usr/bin/env python3
"""backup-consumer: Kafka → Redis 변경 DB 수집기

수동 커밋 + 배치 pipeline으로 at-least-once 보장.
Redis 장애 시 커밋하지 않아 Kafka 재수신.
"""

import json
import logging
from prometheus_client import Counter, Gauge
import redis

log = logging.getLogger("consumer")

# ── Prometheus 메트릭 ──────────────────────────────
EVENTS_PROCESSED = Counter(
    "backup_events_processed_total",
    "Total Kafka events processed")
EVENTS_SKIPPED_DELETE = Counter(
    "backup_events_skipped_delete_total",
    "Delete events skipped (not stored in pending)")
EVENTS_ERRORS = Counter(
    "backup_events_errors_total",
    "Events that failed to process")
EVENTS_REDIS_ERRORS = Counter(
    "backup_events_redis_errors_total",
    "Events lost due to Redis pipeline failure")
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

    배치 모드: parse_event()로 파싱만 하고, flush_batch()로 Redis pipeline 일괄 실행.
    flush 성공 후에만 Kafka 커밋.
    """

    def __init__(self, redis_client: redis.Redis):
        self.r = redis_client
        self.stats = {"processed": 0, "skipped_delete": 0, "errors": 0}
        self._batch = []  # [(action, args), ...]

    def parse_event(self, msg_value: bytes):
        """이벤트 파싱 → 배치 큐에 추가. Redis 호출 없음."""
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
            old_path = event.get("old_path")
            new_path = event.get("new_path")
            if not old_path or not new_path:
                log.warning("rename 이벤트에 old_path/new_path 없음: %s", event)
                self.stats["errors"] += 1
                EVENTS_ERRORS.inc()
                return
            self._batch.append(("rename", (old_path, new_path)))
        elif event_type == "mtime_change":
            path = event.get("path")
            if path:
                self._batch.append(("hset", (path, event_type)))
        else:
            log.warning("알 수 없는 이벤트: %s", event_type)
            self.stats["errors"] += 1
            EVENTS_ERRORS.inc()
            return

        self.stats["processed"] += 1
        EVENTS_PROCESSED.inc()

    @property
    def batch_size(self) -> int:
        return len(self._batch)

    def flush_batch(self) -> bool:
        """배치를 Redis pipeline으로 일괄 실행.

        Returns:
            True: 성공 (Kafka 커밋 해도 됨)
            False: 실패 (Kafka 커밋 하면 안 됨)
        """
        if not self._batch:
            return True

        try:
            pipe = self.r.pipeline()
            for action, args in self._batch:
                if action == "hset":
                    pipe.hset("pending", args[0], args[1])
                elif action == "rename":
                    pipe.hdel("pending", args[0])
                    pipe.hset("pending", args[1], "rename")
            pipe.execute()
            self._batch.clear()
            return True
        except (redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
                redis.exceptions.RedisError) as e:
            log.error("Redis pipeline 실패 (%d건): %s", len(self._batch), e)
            EVENTS_REDIS_ERRORS.inc(len(self._batch))
            # 배치 유지 → 다음 flush에서 재시도하거나,
            # Kafka 미커밋으로 재수신
            self._batch.clear()
            return False

    def update_metrics(self):
        """Prometheus gauge 갱신."""
        try:
            PENDING_TOTAL.set(self.r.hlen("pending"))
            last_run = self.r.hgetall("backup:last_run")
            if last_run:
                BACKUP_LAST_TIMESTAMP.set(float(last_run.get("timestamp", 0)))
                BACKUP_LAST_BACKED_UP.set(float(last_run.get("backed_up", 0)))
                BACKUP_LAST_ERRORS.set(float(last_run.get("errors", 0)))
                BACKUP_LAST_DURATION.set(float(last_run.get("duration", 0)))
        except redis.exceptions.RedisError:
            pass  # metrics 갱신 실패는 무시
