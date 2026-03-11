#!/usr/bin/env python3
"""backup-consumer: Redis pending → restic 증분 백업"""

import json
import os
import subprocess
import tempfile
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


def ensure_repo(repo_url: str, env: dict, initialized: set) -> bool:
    """restic repo가 없으면 init. 이미 확인했으면 skip."""
    if repo_url in initialized:
        return True
    result = subprocess.run(
        ["restic", "cat", "config", "-r", repo_url],
        capture_output=True, env=env
    )
    if result.returncode != 0:
        log.info("repo 초기화: %s", repo_url)
        result = subprocess.run(
            ["restic", "init", "-r", repo_url],
            capture_output=True, env=env
        )
        if result.returncode != 0:
            log.error("repo init 실패: %s: %s", repo_url, result.stderr.decode())
            return False
    initialized.add(repo_url)
    return True


def backup_repo(repo_id: str, files: dict, cfg: dict, env: dict,
                initialized: set) -> dict:
    """단일 repo에 대해 restic backup 실행. 결과 dict 반환."""
    bucket = cfg["minio"]["bucket"]
    endpoint = cfg["minio"]["endpoint"]
    repo_url = f"s3:http://{endpoint}/{bucket}/{repo_id}"

    result = {"repo_id": repo_id, "total": len(files),
              "backed_up": 0, "deleted": 0, "errors": 0}

    if not ensure_repo(repo_url, env, initialized):
        result["errors"] = len(files)
        return result

    # mtime_change, rename → 백업 대상 (파일이 존재하는 것만)
    backup_list = []
    for path, info_str in files.items():
        info = json.loads(info_str)
        if info["event"] == "delete":
            result["deleted"] += 1
            continue
        if os.path.exists(path):
            backup_list.append(path)
        else:
            log.debug("파일 없음 (이미 삭제?): %s", path)

    if backup_list:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("\n".join(backup_list))
            list_file = f.name

        try:
            proc = subprocess.run(
                ["restic", "backup", "--files-from", list_file,
                 "-r", repo_url, "--tag", "incremental"],
                capture_output=True, env=env, timeout=3600
            )
            if proc.returncode == 0:
                result["backed_up"] = len(backup_list)
                log.info("repo=%s 백업 완료: %d건", repo_id, len(backup_list))
            else:
                stderr = proc.stderr.decode()
                # 파일 없는 경고는 무시 (백업 중 삭제될 수 있음)
                if proc.returncode == 3:  # restic: incomplete snapshot
                    result["backed_up"] = len(backup_list)
                    log.warning("repo=%s 일부 파일 누락 (incomplete): %d건",
                                repo_id, len(backup_list))
                else:
                    log.error("repo=%s 백업 실패: %s", repo_id, stderr[:200])
                    result["errors"] = len(backup_list)
        finally:
            os.unlink(list_file)
    else:
        log.info("repo=%s 백업 대상 없음 (delete만 %d건)", repo_id, result["deleted"])

    return result


def run_backup(cfg: dict, r: redis.Redis):
    """전체 백업 실행: Redis pending → repo별 그룹핑 → 병렬 restic backup."""

    # 1. Redis에서 전체 pending 추출
    all_items = r.hgetall("pending")
    if not all_items:
        log.info("백업 대상 없음")
        return

    log.info("총 %d건 pending", len(all_items))

    # 2. repo별 그룹핑
    base = cfg["backup"]["base_path"]
    depth = cfg["backup"]["repo_depth"]
    tasks = {}  # {repo_id: {path: info}}
    skipped = []

    for path, info in all_items.items():
        repo_id = get_repo_id(path, base, depth)
        if repo_id is None:
            skipped.append(path)
            continue
        tasks.setdefault(repo_id, {})[path] = info

    if skipped:
        log.warning("repo 매핑 실패 (depth 부족): %d건", len(skipped))

    log.info("repo %d개로 분배", len(tasks))

    # 3. restic 환경변수
    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = cfg["backup"]["restic_password"]
    env["AWS_ACCESS_KEY_ID"] = cfg["minio"]["access_key"]
    env["AWS_SECRET_ACCESS_KEY"] = cfg["minio"]["secret_key"]

    initialized = set()
    workers = cfg["backup"].get("workers", 4)

    # 4. Worker pool로 병렬 실행
    total_stats = {"backed_up": 0, "deleted": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(backup_repo, repo_id, files, cfg, env, initialized): repo_id
            for repo_id, files in tasks.items()
        }

        for future in as_completed(futures):
            repo_id = futures[future]
            try:
                result = future.result()
                total_stats["backed_up"] += result["backed_up"]
                total_stats["deleted"] += result["deleted"]
                total_stats["errors"] += result["errors"]

                # 성공한 항목만 Redis에서 제거
                if result["errors"] == 0:
                    for path in tasks[repo_id]:
                        r.hdel("pending", path)
            except Exception as e:
                log.error("repo=%s 예외: %s", repo_id, e)
                total_stats["errors"] += len(tasks[repo_id])

    # 5. skipped 항목도 Redis에서 제거 (repo 매핑 안 되는 경로)
    for path in skipped:
        r.hdel("pending", path)

    remaining = r.hlen("pending")
    log.info("백업 완료. 백업: %d, 삭제기록: %d, 에러: %d, 남은 pending: %d",
             total_stats["backed_up"], total_stats["deleted"],
             total_stats["errors"], remaining)


def main():
    import sys
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)
    r.ping()
    log.info("Redis 연결 OK, pending: %d건", r.hlen("pending"))

    run_backup(cfg, r)


if __name__ == "__main__":
    main()
