#!/usr/bin/env python3
"""모든 restic repo에서 오래된 스냅샷 정리."""

import os
import subprocess
import sys
import logging
from minio import Minio
import toml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("prune")


def restic_env():
    env = os.environ.copy()
    for key in ("RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if key not in env:
            raise RuntimeError(f"환경변수 필요: {key}")
    return env


def prune_all_repos(cfg: dict):
    env = restic_env()
    endpoint = cfg["minio"]["endpoint"]
    bucket = cfg["minio"]["bucket"]
    use_tls = cfg["minio"].get("use_tls", False)
    keep_days = cfg.get("prune", {}).get("keep_days", 90)
    restic_bin = cfg["backup"].get("restic_binary", "restic")

    client = Minio(
        endpoint,
        access_key=env["AWS_ACCESS_KEY_ID"],
        secret_key=env["AWS_SECRET_ACCESS_KEY"],
        secure=use_tls
    )

    repos = set()
    for obj in client.list_objects(bucket, recursive=True):
        if obj.object_name.endswith("/config"):
            repo_prefix = obj.object_name.rsplit("/config", 1)[0]
            if repo_prefix:
                repos.add(repo_prefix)

    scheme = "https" if use_tls else "http"
    log.info("prune 대상 repo: %d개", len(repos))
    for repo_prefix in sorted(repos):
        repo_url = f"s3:{scheme}://{endpoint}/{bucket}/{repo_prefix}"
        log.info("prune: %s (keep %dd)", repo_prefix, keep_days)
        result = subprocess.run(
            [restic_bin, "forget", f"--keep-within={keep_days}d",
             "--prune", "-r", repo_url],
            capture_output=True, env=env
        )
        if result.returncode != 0:
            log.error("prune 실패 %s: %s", repo_prefix,
                      result.stderr.decode()[:200])


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)
    prune_all_repos(cfg)
    log.info("prune 완료")


if __name__ == "__main__":
    main()
