#!/usr/bin/env python3
"""backup-consumer CLI: 스냅샷 조회, 복원, 상태 확인"""

import argparse
import json
import os
import subprocess
import sys
from minio import Minio
import redis
import toml


def load_config(args):
    cfg = toml.load(args.config)
    return cfg


def restic_env(cfg):
    env = os.environ.copy()
    if "RESTIC_PASSWORD" not in env:
        env["RESTIC_PASSWORD"] = cfg["backup"].get("restic_password", "")
    if "AWS_ACCESS_KEY_ID" not in env:
        env["AWS_ACCESS_KEY_ID"] = cfg["minio"].get("access_key", "")
    if "AWS_SECRET_ACCESS_KEY" not in env:
        env["AWS_SECRET_ACCESS_KEY"] = cfg["minio"].get("secret_key", "")
    return env


def restic_bin(cfg):
    return cfg["backup"].get("restic_binary", "restic")


def repo_url(cfg, repo_id):
    endpoint = cfg["minio"]["endpoint"]
    bucket = cfg["minio"]["bucket"]
    return f"s3:http://{endpoint}/{bucket}/{repo_id}"


def get_repo_id(path, base, depth):
    if not path.startswith(base + "/"):
        return None
    rel = path[len(base) + 1:]
    parts = rel.split("/")
    if len(parts) < depth:
        return None
    return "/".join(parts[:depth])


def list_repos(cfg, env):
    """MinIO에서 restic repo 목록 조회."""
    endpoint = cfg["minio"]["endpoint"]
    bucket = cfg["minio"]["bucket"]
    use_tls = cfg["minio"].get("use_tls", False)

    client = Minio(
        endpoint,
        access_key=env.get("AWS_ACCESS_KEY_ID", ""),
        secret_key=env.get("AWS_SECRET_ACCESS_KEY", ""),
        secure=use_tls
    )
    repos = set()
    for obj in client.list_objects(bucket, recursive=True):
        if obj.object_name.endswith("/config"):
            repo_prefix = obj.object_name.rsplit("/config", 1)[0]
            if repo_prefix:
                repos.add(repo_prefix)
    return sorted(repos)


# ── status ──────────────────────────────────────────

def cmd_status(args):
    cfg = load_config(args)
    env = restic_env(cfg)

    r = redis.Redis.from_url(cfg["db"]["redis_url"], decode_responses=True)

    pending = r.hlen("pending")
    print(f"Pending events: {pending}")

    if args.verbose and pending > 0:
        all_items = r.hgetall("pending")
        by_event = {}
        for _, info_str in all_items.items():
            info = json.loads(info_str)
            ev = info.get("event", "unknown")
            by_event[ev] = by_event.get(ev, 0) + 1
        for ev, count in sorted(by_event.items()):
            print(f"  {ev}: {count}")

    repos = list_repos(cfg, env)
    print(f"Repos: {len(repos)}")
    for repo_id in repos:
        if args.verbose:
            url = repo_url(cfg, repo_id)
            result = subprocess.run(
                [restic_bin(cfg), "snapshots", "--latest=1", "--compact",
                 "-r", url, "--json"],
                capture_output=True, text=True, env=env
            )
            last = ""
            if result.returncode == 0:
                snaps = json.loads(result.stdout)
                if snaps:
                    last = snaps[-1].get("time", "")[:19]
            print(f"  {repo_id}  (last: {last})")
        else:
            print(f"  {repo_id}")


# ── snapshots ───────────────────────────────────────

def cmd_snapshots(args):
    cfg = load_config(args)
    env = restic_env(cfg)
    base = cfg["backup"]["base_path"]
    depth = cfg["backup"]["repo_depth"]

    if args.path:
        rid = get_repo_id(args.path, base, depth)
        if not rid:
            print(f"Error: cannot determine repo from path: {args.path}", file=sys.stderr)
            sys.exit(1)
    elif args.repo:
        rid = args.repo
    else:
        print("Error: --path or --repo required", file=sys.stderr)
        sys.exit(1)

    url = repo_url(cfg, rid)
    cmd = [restic_bin(cfg), "snapshots", "-r", url]
    if args.json_output:
        cmd.append("--json")
    subprocess.run(cmd, env=env)


# ── restore ─────────────────────────────────────────

def cmd_restore(args):
    cfg = load_config(args)
    env = restic_env(cfg)
    base = cfg["backup"]["base_path"]
    depth = cfg["backup"]["repo_depth"]

    rid = get_repo_id(args.path, base, depth)
    if not rid:
        print(f"Error: cannot determine repo from path: {args.path}", file=sys.stderr)
        sys.exit(1)

    url = repo_url(cfg, rid)
    target = args.target or "/tmp/restore"

    cmd = [restic_bin(cfg), "restore", args.snapshot or "latest",
           "--target", target,
           "--include", args.path,
           "-r", url]
    print(f"Repo: {rid}")
    print(f"Restoring to: {target}")
    sys.stdout.flush()
    result = subprocess.run(cmd, env=env)
    if result.returncode == 0:
        print(f"Done. Check: {target}{args.path}")
    else:
        sys.exit(result.returncode)


# ── main ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="backup-cli",
        description="backup-consumer CLI: 스냅샷 조회, 복원, 상태 확인"
    )
    parser.add_argument("-c", "--config", default="config.toml",
                        help="설정 파일 경로")
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="시스템 상태 확인")
    p_status.add_argument("-v", "--verbose", action="store_true")

    # snapshots
    p_snap = sub.add_parser("snapshots", help="스냅샷 목록 조회")
    p_snap.add_argument("--path", help="파일 경로 (repo 자동 결정)")
    p_snap.add_argument("--repo", help="repo ID 직접 지정")
    p_snap.add_argument("--json", dest="json_output", action="store_true")

    # restore
    p_restore = sub.add_parser("restore", help="파일/디렉토리 복원")
    p_restore.add_argument("path", help="복원할 파일 경로")
    p_restore.add_argument("-s", "--snapshot", help="스냅샷 ID (기본: latest)")
    p_restore.add_argument("-t", "--target", help="복원 대상 경로 (기본: /tmp/restore)")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "snapshots":
        cmd_snapshots(args)
    elif args.command == "restore":
        cmd_restore(args)


if __name__ == "__main__":
    main()
