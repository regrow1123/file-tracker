# backup-consumer

Kafka 이벤트 소비 → Redis 변경 DB → restic 증분 백업 (MinIO/S3)

## 아키텍처

```
file-tracker (각 노드) → Kafka → backup-consumer → restic → MinIO
                                      ↕
                                    Redis (pending Hash)
```

## 서비스 구성

| 서비스 | 타입 | 역할 |
|--------|------|------|
| `backup-consumer.service` | 상시 실행 | Kafka poll → Redis HSET |
| `backup-run.timer` | 매일 03:00 | Redis → repo별 restic 증분 백업 |
| `backup-prune.timer` | 매주 일 04:00 | 오래된 스냅샷 정리 (90일 보존) |

## 파일 구성

| 파일 | 역할 |
|------|------|
| `main.py` | consumer 데몬 (Kafka → Redis) |
| `consumer.py` | EventProcessor 라이브러리 |
| `backup.py` | 증분 백업 (oneshot, timer에서 호출) |
| `prune.py` | 스냅샷 정리 (oneshot, timer에서 호출) |
| `cli.py` | CLI: 상태, 스냅샷, 복원 |
| `config.toml` | 설정 (시크릿 미포함) |

## 의존성

- Python 3.9+
- Redis
- MinIO (S3)
- restic

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
```

`/etc/backup-consumer/env` (시크릿, chmod 600):

```
RESTIC_PASSWORD=your-password
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
```

## 설치

```bash
cd consumer
sudo bash deploy/install.sh
sudo vi /etc/backup-consumer/env          # 시크릿 수정
sudo vi /etc/backup-consumer/config.toml  # 설정 수정

# 시작
sudo systemctl start backup-consumer
sudo systemctl enable --now backup-run.timer
sudo systemctl enable --now backup-prune.timer
```

## 상태 확인

```bash
# 서비스 상태
systemctl status backup-consumer
systemctl list-timers backup-*

# 로그
journalctl -u backup-consumer -f   # consumer
journalctl -u backup-run -f        # 백업
journalctl -u backup-prune -f      # prune
```

## CLI

```bash
# 상태 (pending 건수, repo 목록)
python3 cli.py status
python3 cli.py status -v

# 스냅샷 조회 (경로로 repo 자동 결정)
python3 cli.py snapshots --path /home/stor1/userA/docs/a.txt
python3 cli.py snapshots --repo stor1/userA

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

## 수동 실행

```bash
# 수동 백업 트리거
export RESTIC_PASSWORD="..."
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
python3 backup.py config.toml

# 수동 prune
python3 prune.py config.toml

# restic 직접 사용
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA snapshots
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA restore latest --target /restore
```

## 설계 결정

- **delete 이벤트 무시**: 삭제된 파일은 이전 스냅샷에 보존. 보존기간 후 prune에서 정리
- **ts 비교 없음**: pending은 "이 파일 확인" 마커. 백업 시 Lustre 현재 상태를 직접 읽으므로 이벤트 순서 무관
- **RENAME 배치 처리**: `pending` → `processing` 원자적 swap으로 consumer와 backup 간 레이스 제거
- **HSCAN 배치**: Redis 블로킹 방지 (1만 건씩)
- **시크릿 분리**: EnvironmentFile로 config.toml에서 비밀번호 제거
