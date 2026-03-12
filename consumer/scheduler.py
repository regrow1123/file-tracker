#!/usr/bin/env python3
"""backup-scheduler: cron 기반 restic 백업 + 스냅샷 정리 데몬

consumer(main.py)와 별도 서비스로 실행.
시크릿(RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)은
환경변수에서 읽는다.
"""

import os
import signal
import subprocess
import sys
import threading
import logging
from datetime import datetime
from croniter import croniter
import redis
import toml

from backup import run_backup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("scheduler")


class Scheduler:
    """cron 표현식 기반 스케줄러."""

    def __init__(self):
        self._jobs = []
        self._stop = threading.Event()

    def add(self, cron_expr: str, callback, name: str):
        cron = croniter(cron_expr, datetime.now())
        next_time = cron.get_next(datetime)
        self._jobs.append((cron, callback, name))
        log.info("스케줄 등록: %s → 다음 실행 %s", name, next_time)

    def run(self):
        while not self._stop.is_set():
            now = datetime.now()
            for cron, callback, name in self._jobs:
                next_time = cron.get_current(datetime)
                if now >= next_time:
                    log.info("스케줄 실행: %s", name)
                    try:
                        callback()
                    except Exception as e:
                        log.error("스케줄 %s 실패: %s", name, e)
                    cron.get_next(datetime)
            self._stop.wait(30)

    def stop(self):
        self._stop.set()


def prune_all_repos(cfg: dict):
    """모든 repo에서 오래된 스냅샷 정리."""
    env = _restic_env()
    endpoint = cfg["minio"]["endpoint"]
    bucket = cfg["minio"]["bucket"]
    keep_days = cfg.get("prune", {}).get("keep_days", 90)

    result = subprocess.run(
        ["mc", "ls", f"local/{bucket}/", "--recursive"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log.error("repo 목록 조회 실패: %s", result.stderr)
        return

    repos = set()
    for line in result.stdout.strip().split("\n"):
        if line.strip().endswith("/config"):
            parts = line.strip().split()
            path = parts[-1] if parts else ""
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


def _restic_env():
    """환경변수에서 시크릿 읽기."""
    env = os.environ.copy()
    for key in ("RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if key not in env:
            log.error("환경변수 누락: %s", key)
            raise RuntimeError(f"환경변수 필요: {key}")
    return env


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    # 시크릿 환경변수 검증
    _restic_env()

    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)
    r.ping()
    log.info("Redis OK, pending: %d건", r.hlen("pending"))

    scheduler = Scheduler()
    scheduler.add(
        cfg["backup"]["schedule"],
        lambda: run_backup(cfg, r),
        "증분 백업"
    )
    scheduler.add(
        cfg.get("prune", {}).get("schedule", "0 4 * * 0"),
        lambda: prune_all_repos(cfg),
        "스냅샷 정리"
    )

    running = True
    def sig_handler(sig, frame):
        nonlocal running
        running = False
        scheduler.stop()
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    log.info("backup-scheduler 시작")
    scheduler.run()
    log.info("backup-scheduler 종료")


if __name__ == "__main__":
    main()
