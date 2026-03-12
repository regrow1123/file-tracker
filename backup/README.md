# backup (증분 스냅샷)

> **참고**: 이 컴포넌트는 file-tracker 프로젝트에서 분리된 백업 모듈입니다.
> Redis `pending` Hash를 입력으로 받아 restic 증분 백업을 수행합니다.

## 아키텍처

```
Redis pending Hash → backup.py → restic --files-from → MinIO (S3)
                     prune.py  → restic forget --prune
                     cli.py    → status / snapshots / restore
```

## 전제 조건

- Redis에 `pending` Hash가 존재 (backup-consumer가 Kafka → Redis 수집)
- Lustre 공유 마운트 (파일에 직접 접근 가능)
- MinIO (S3) 접근 가능
- restic 설치

## 파일 구성

| 파일 | 역할 |
|------|------|
| `backup.py` | 증분 백업: 분산 락, RENAME, HSCAN, ThreadPool, restic 재시도 |
| `prune.py` | 스냅샷 정리: 분산 락, MinIO SDK repo 탐색, restic forget |
| `cli.py` | CLI: status, snapshots, restore |
| `config.toml` | 설정 (시크릿 미포함) |

## 설정

`config.toml`:

```toml
[backup]
restic_binary = "/usr/bin/restic"
workers = 4
base_path = "/home"
repo_depth = 2

[minio]
endpoint = "minio:9000"
bucket = "file-tracker-backup"
use_tls = false

[db]
redis_url = "redis://localhost:6379/0"

[prune]
keep_days = 90

[logging]
level = "INFO"
```

시크릿 (`/etc/backup-consumer/env`, chmod 600):

```
RESTIC_PASSWORD=your-password
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
```

## 실행

```bash
# 환경 변수
export RESTIC_PASSWORD="..."
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin

# 백업 (oneshot)
python3 backup.py config.toml

# prune (oneshot)
python3 prune.py config.toml

# CLI
python3 cli.py status -v
python3 cli.py snapshots --path /home/stor1/userA/docs/a.txt
python3 cli.py restore /home/stor1/userA/docs/a.txt -t /restore
```

## systemd

```bash
sudo cp deploy/backup-run.service deploy/backup-run.timer /etc/systemd/system/
sudo cp deploy/backup-prune.service deploy/backup-prune.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now backup-run.timer
sudo systemctl enable --now backup-prune.timer
```

## Docker (MinIO)

테스트용 MinIO:

```bash
docker compose -f docker-compose.yml up -d
# MinIO console: http://localhost:9001 (minioadmin/minioadmin)
```

## 분산 락

backup과 prune은 Redis 분산 락(`backup:lock`)을 사용하여 동시 실행을 방지합니다.

| 시나리오 | 결과 |
|---------|------|
| 노드A backup + 노드B backup | B 스킵 |
| backup + prune 동시 | prune 스킵 |
| 프로세스 kill | TTL 2시간 후 자동 해제 |
