# backup-consumer

Kafka 이벤트 소비 → Redis 변경 DB → restic 증분 백업 (MinIO/S3)

## 아키텍처

```
file-tracker (각 노드) → Kafka → backup-consumer → restic → MinIO
                                      ↕
                                    Redis (pending Hash)
```

## 구성 요소

| 파일 | 역할 |
|------|------|
| `main.py` | 통합 데몬: consumer + 스케줄러 |
| `consumer.py` | Kafka → Redis 이벤트 수집 |
| `backup.py` | Redis → repo별 restic 증분 백업 |
| `cli.py` | CLI: 상태, 스냅샷, 복원 |
| `config.toml` | 설정 파일 |

## 의존성

- Python 3.9+
- Redis
- MinIO (S3)
- restic
- mc (MinIO client, prune 시 repo 탐색용)

```bash
pip install -r requirements.txt
```

## 설정

`/etc/backup-consumer/config.toml`:

```toml
[kafka]
brokers = "kafka01:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
schedule = "0 3 * * *"              # 매일 새벽 3시
restic_binary = "/usr/bin/restic"
restic_password = "your-password"
workers = 4
base_path = "/home"
repo_depth = 2                      # /home/{stor}/{user} 단위 repo

[minio]
endpoint = "minio:9000"
bucket = "file-tracker-backup"
access_key = "minioadmin"
secret_key = "minioadmin"

[db]
redis_url = "redis://localhost:6379/0"

[prune]
schedule = "0 4 * * 0"              # 매주 일요일 새벽 4시
keep_days = 90
```

## 설치

```bash
cd consumer
sudo bash deploy/install.sh
# 설정 수정
sudo vi /etc/backup-consumer/config.toml
# 시작
sudo systemctl start backup-consumer
```

## CLI 사용

```bash
# 상태 확인
python3 cli.py status
python3 cli.py status -v

# 스냅샷 조회 (경로로 repo 자동 결정)
python3 cli.py snapshots --path /home/stor1/userA/docs/a.txt

# 복원
python3 cli.py restore /home/stor1/userA/docs/a.txt
python3 cli.py restore /home/stor1/userA/docs/a.txt -s abc123 -t /restore
```

## repo 구조

경로의 depth=2로 자동 분리:

```
/home/stor1/userA/docs/a.txt → repo: stor1/userA
/home/stor1/userB/data/b.txt → repo: stor1/userB
```

MinIO:
```
s3://file-tracker-backup/
  stor1/userA/ ← restic repo
  stor1/userB/ ← restic repo
```

## 수동 백업/복원

```bash
# 수동 백업 트리거
python3 backup.py config.toml

# restic 직접 사용
export RESTIC_PASSWORD="..."
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA snapshots
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA restore latest --target /restore
```

## 로그

```bash
journalctl -u backup-consumer -f
```
