#!/usr/bin/env python3
"""backup-consumer: Redis pending → restic 증분 백업

RENAME 방식: pending → processing 원자적 swap으로 consumer와 간섭 차단.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import redis
import toml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("backup")


def get_repo_id(path: str, base: str, depth: int) -> str | None:
    """경로에서 repo_id 추출. depth=2이면 /home/stor1/userA → stor1/userA"""
    if not path.startswith(base + "/"):
        return None
    rel = path[len(base) + 1:]
    parts = rel.split("/")
    if len(parts) < depth:
        return None
    return "/".join(parts[:depth])


class RepoManager:
    """restic repo 초기화 관리. thread-safe."""

    def __init__(self, restic_bin: str = "restic"):
        self._initialized = set()
        self._lock = threading.Lock()
        self._restic = restic_bin

    def ensure(self, repo_url: str, env: dict) -> bool:
        with self._lock:
            if repo_url in self._initialized:
                return True
            # repo별 lock: 같은 repo의 중복 init 방지
            if not hasattr(self, '_pending'):
                self._pending = set()
            if repo_url in self._pending:
                # 다른 스레드가 init 중 → 완료 대기
                pass
            self._pending.add(repo_url)

        # lock 밖에서 restic 호출 (I/O 블로킹)
        try:
            result = subprocess.run(
                [self._restic, "cat", "config", "-r", repo_url],
                capture_output=True, env=env
            )
            if result.returncode != 0:
                log.info("repo 초기화: %s", repo_url)
                result = subprocess.run(
                    [self._restic, "init", "-r", repo_url],
                    capture_output=True, env=env
                )
                if result.returncode != 0:
                    log.error("repo init 실패: %s: %s",
                              repo_url, result.stderr.decode())
                    return False

            with self._lock:
                self._initialized.add(repo_url)
            return True
        finally:
            with self._lock:
                self._pending.discard(repo_url)


def backup_repo(repo_id: str, paths: list[str], cfg: dict, env: dict,
                repo_mgr: RepoManager, restic_bin: str = "restic") -> dict:
    """단일 repo에 대해 restic backup 실행."""
    endpoint = cfg["minio"]["endpoint"]
    bucket = cfg["minio"]["bucket"]
    use_tls = cfg["minio"].get("use_tls", False)
    scheme = "https" if use_tls else "http"
    repo_url = f"s3:{scheme}://{endpoint}/{bucket}/{repo_id}"

    result = {"repo_id": repo_id, "total": len(paths),
              "backed_up": 0, "skipped": 0, "errors": 0}

    if not repo_mgr.ensure(repo_url, env):
        result["errors"] = len(paths)
        return result

    # 존재하는 파일만 필터
    existing = [p for p in paths if os.path.exists(p)]
    result["skipped"] = len(paths) - len(existing)

    if not existing:
        log.info("repo=%s 백업 대상 없음 (모두 삭제됨)", repo_id)
        return result

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False) as f:
        f.write("\n".join(existing))
        list_file = f.name

    try:
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            proc = subprocess.run(
                [restic_bin, "backup", "--files-from", list_file,
                 "-r", repo_url, "--tag", "incremental"],
                capture_output=True, env=env, timeout=3600
            )
            if proc.returncode in (0, 3):
                result["backed_up"] = len(existing)
                if proc.returncode == 3:
                    log.warning("repo=%s 일부 파일 누락: %d건",
                                repo_id, len(existing))
                else:
                    log.info("repo=%s 백업 완료: %d건", repo_id, len(existing))
                break
            else:
                stderr = proc.stderr.decode()[:200]
                if attempt < max_retries:
                    delay = attempt * 10  # 10s, 20s
                    log.warning("repo=%s 백업 실패 (시도 %d/%d), %ds 후 재시도: %s",
                                repo_id, attempt, max_retries, delay, stderr)
                    time.sleep(delay)
                else:
                    log.error("repo=%s 백업 최종 실패: %s", repo_id, stderr)
                    result["errors"] = len(existing)
    finally:
        os.unlink(list_file)

    return result


def run_backup(cfg: dict, r: redis.Redis):
    """전체 백업: RENAME으로 pending 스냅샷 → repo별 병렬 restic backup."""

    start_time = time.time()

    # 0. 이전 실행이 중단되어 processing이 남아있으면 이어서 처리
    if r.exists("processing"):
        log.warning("이전 processing 키 발견 — 중단된 백업 이어서 처리")
    else:
        # 1. 원자적 swap: pending → processing
        try:
            r.rename("pending", "processing")
        except redis.exceptions.ResponseError:
            log.info("백업 대상 없음 (pending 비어있음)")
            return

    # 이 시점부터 consumer는 새 pending에 쓰기 (HSET이 자동 생성)

    # 2. processing에서 HSCAN으로 배치 추출 (Redis 블로킹 방지)
    base = cfg["backup"]["base_path"]
    depth = cfg["backup"]["repo_depth"]
    tasks = {}  # {repo_id: [path, ...]}
    skipped = 0
    total_count = 0

    cursor = 0
    while True:
        cursor, items = r.hscan("processing", cursor, count=10000)
        for path in items:
            total_count += 1
            repo_id = get_repo_id(path, base, depth)
            if repo_id is None:
                skipped += 1
                continue
            tasks.setdefault(repo_id, []).append(path)
        if cursor == 0:
            break

    log.info("총 %d건 processing", total_count)

    if skipped:
        log.warning("repo 매핑 실패 (depth 부족): %d건", skipped)

    log.info("repo %d개로 분배", len(tasks))

    # 4. restic 환경변수 (시크릿은 환경변수에서, 없으면 config fallback)
    env = os.environ.copy()
    if "RESTIC_PASSWORD" not in env:
        env["RESTIC_PASSWORD"] = cfg["backup"].get("restic_password", "")
    if "AWS_ACCESS_KEY_ID" not in env:
        env["AWS_ACCESS_KEY_ID"] = cfg["minio"].get("access_key", "")
    if "AWS_SECRET_ACCESS_KEY" not in env:
        env["AWS_SECRET_ACCESS_KEY"] = cfg["minio"].get("secret_key", "")

    restic_bin = cfg["backup"].get("restic_binary", "restic")
    repo_mgr = RepoManager(restic_bin)
    workers = cfg["backup"].get("workers", 4)

    # 5. Worker pool로 병렬 실행
    total = {"backed_up": 0, "skipped": 0, "errors": 0}
    failed_repos = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(backup_repo, repo_id, paths, cfg, env,
                        repo_mgr, restic_bin): repo_id
            for repo_id, paths in tasks.items()
        }
        for future in as_completed(futures):
            repo_id = futures[future]
            try:
                res = future.result()
                total["backed_up"] += res["backed_up"]
                total["skipped"] += res["skipped"]
                total["errors"] += res["errors"]
                if res["errors"] > 0:
                    failed_repos.append(repo_id)
            except Exception as e:
                log.error("repo=%s 예외: %s", repo_id, e)
                total["errors"] += len(tasks[repo_id])
                failed_repos.append(repo_id)

    # 6. 실패한 repo의 경로를 pending에 복원 (다음 타이머에서 재시도)
    if failed_repos:
        pipe = r.pipeline()
        restored = 0
        for repo_id in failed_repos:
            for path in tasks[repo_id]:
                pipe.hset("pending", path, "retry")
                restored += 1
        pipe.execute()
        log.warning("실패 repo %d개, %d건 pending에 복원",
                    len(failed_repos), restored)

    # 7. processing 삭제
    r.delete("processing")

    # 8. 백업 결과를 Redis에 기록 (Prometheus exporter용)
    duration = time.time() - start_time
    r.hset("backup:last_run", mapping={
        "timestamp": str(int(time.time())),
        "backed_up": str(total["backed_up"]),
        "skipped": str(total["skipped"]),
        "errors": str(total["errors"]),
        "repos": str(len(tasks)),
        "duration": f"{duration:.1f}",
    })

    log.info("백업 완료. 백업: %d, 스킵: %d, 에러: %d, 소요: %.1fs",
             total["backed_up"], total["skipped"], total["errors"], duration)


def main():
    import sys
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True
    )

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            r = redis.Redis.from_url(cfg["db"]["redis_url"],
                                     decode_responses=True,
                                     socket_connect_timeout=5,
                                     socket_timeout=10)
            r.ping()
            break
        except (redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError) as e:
            if attempt == max_retries:
                log.error("Redis 연결 실패 (%d회 시도): %s", max_retries, e)
                sys.exit(1)
            delay = attempt * 10
            log.warning("Redis 연결 실패 (시도 %d/%d), %ds 후 재시도: %s",
                        attempt, max_retries, delay, e)
            time.sleep(delay)

    log.info("Redis 연결 OK, pending: %d건", r.hlen("pending"))
    run_backup(cfg, r)


if __name__ == "__main__":
    main()
