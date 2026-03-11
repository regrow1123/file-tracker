#!/usr/bin/env python3
"""backup-consumer: 통합 데몬
- Kafka consumer: 상시 실행, 이벤트 → Redis
- Backup scheduler: cron 스케줄에 따라 restic 백업
- Prune scheduler: 오래된 스냅샷 정리
"""

import json
import signal
import sys
import os
import subprocess
import threading
import logging
from datetime import datetime
from croniter import croniter
import redis
import toml

from consumer import EventProcessor
from backup import run_backup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")


class Scheduler:
    """cron 표현식 기반 스케줄러. 별도 스레드에서 실행."""

    def __init__(self):
        self._jobs = []  # [(croniter, callback, name)]
        self._stop = threading.Event()

    def add(self, cron_expr: str, callback, name: str):
        cron = croniter(cron_expr, datetime.now())
        self._jobs.append((cron, callback, name))
        next_time = cron.get_next(datetime)
        log.info("스케줄 등록: %s → 다음 실행 %s", name, next_time)

    def run(self):
        """스케줄 루프. _stop이 set되면 종료."""
        while not self._stop.is_set():
            now = datetime.now()
            for i, (cron, callback, name) in enumerate(self._jobs):
                next_time = cron.get_current(datetime)
                if now >= next_time:
                    log.info("스케줄 실행: %s", name)
                    try:
                        callback()
                    except Exception as e:
                        log.error("스케줄 %s 실패: %s", name, e)
                    # 다음 실행 시각 갱신
                    cron.get_next(datetime)
            self._stop.wait(30)  # 30초마다 체크

    def stop(self):
        self._stop.set()


def prune_all_repos(cfg: dict):
    """모든 repo에서 오래된 스냅샷 정리."""
    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = cfg["backup"]["restic_password"]
    env["AWS_ACCESS_KEY_ID"] = cfg["minio"]["access_key"]
    env["AWS_SECRET_ACCESS_KEY"] = cfg["minio"]["secret_key"]

    bucket = cfg["minio"]["bucket"]
    endpoint = cfg["minio"]["endpoint"]
    keep_days = cfg["backup"].get("keep_days", 90)

    # mc ls로 repo 목록 조회
    result = subprocess.run(
        ["mc", "ls", f"local/{bucket}/", "--recursive"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log.error("repo 목록 조회 실패: %s", result.stderr)
        return

    # config 파일이 있는 prefix = restic repo
    repos = set()
    for line in result.stdout.strip().split("\n"):
        if line.endswith("/config"):
            # "... 123B standard config" → prefix 추출
            parts = line.strip().split()
            path = parts[-1] if parts else ""
            # path: stor1/userA/config
            repo_prefix = path.rsplit("/config", 1)[0]
            if repo_prefix:
                repos.add(repo_prefix)

    log.info("prune 대상 repo: %d개", len(repos))

    for repo_prefix in repos:
        repo_url = f"s3:http://{endpoint}/{bucket}/{repo_prefix}"
        log.info("prune: %s (keep %dd)", repo_prefix, keep_days)
        subprocess.run(
            ["restic", "forget", f"--keep-within={keep_days}d",
             "--prune", "-r", repo_url],
            capture_output=True, env=env
        )


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    # Redis
    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)
    r.ping()
    log.info("Redis OK, pending: %d건", r.hlen("pending"))

    # Kafka consumer (confluent_kafka)
    from confluent_kafka import Consumer, KafkaError
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

    # 스케줄러
    scheduler = Scheduler()
    scheduler.add(
        cfg["backup"]["schedule"],
        lambda: run_backup(cfg, r),
        "증분 백업"
    )
    scheduler.add(
        cfg.get("prune", {}).get("schedule", "0 4 * * 0"),  # 기본: 매주 일 04시
        lambda: prune_all_repos(cfg),
        "스냅샷 정리"
    )

    sched_thread = threading.Thread(target=scheduler.run, daemon=True)
    sched_thread.start()

    # graceful shutdown
    running = True
    def sig_handler(sig, frame):
        nonlocal running
        running = False
        scheduler.stop()
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # 메인 루프: Kafka poll
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
                log.info("이벤트: %d건, pending: %d",
                         processor.stats["processed"], pending)
    finally:
        kafka_consumer.close()
        log.info("종료. 총 이벤트: %d건", processor.stats["processed"])


if __name__ == "__main__":
    main()
