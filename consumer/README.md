# backup-consumer

Kafka 이벤트 소비 → Redis 변경 DB → restic 증분 백업 (MinIO/S3)

## 특징

- **at-least-once 보장**: 수동 Kafka 커밋 — Redis 쓰기 성공 후에만 offset 커밋
- **배치 파이프라인**: 100건 또는 5초 간격으로 Redis pipeline 일괄 실행
- **분산 락**: Redis SETNX로 멀티노드 동시 실행 방지
- **자동 장애 복구**: Kafka/Redis 재연결, processing 복구, restic 재시도
- **Prometheus 모니터링**: 이벤트 처리량, pending 건수, 백업 결과, Kafka 에러
- **수평 확장**: Kafka consumer group 기반 — 프로세스 추가만으로 스케일 아웃

## 아키텍처

```
file-tracker (각 노드)
    │
    ▼
  Kafka (10 파티션, 파티션 키=hostname)
    │
    ▼
backup-consumer.service (상시 실행, 수평 확장 가능)
  │  Kafka poll → parse_event (배치 큐)
  │  → 100건/5초 도달 → Redis pipeline flush
  │  → 성공 → Kafka commit
  │  → 실패 → commit 안 함 → Kafka 재수신
  │
  ▼
Redis (pending Hash)
  │
  ▼
backup-run.timer (매일 03:00)
  │  분산 락 획득 → RENAME pending→processing
  │  → HSCAN → repo별 그룹핑 → ThreadPoolExecutor
  │  → restic --files-from → MinIO
  │  → 실패: 3회 재시도 → pending 복원
  │
  ▼
MinIO (S3) — 단일 버킷, prefix별 restic repo
  │
  ▼
backup-prune.timer (매주 일 04:00)
    분산 락 획득 → restic forget --keep-within=90d --prune
```

## 서비스 구성

| 서비스 | 타입 | 역할 |
|--------|------|------|
| `backup-consumer.service` | 상시 실행 | Kafka poll → Redis pipeline (수동 커밋) |
| `backup-run.timer` | 매일 03:00 | Redis → repo별 restic 증분 백업 |
| `backup-prune.timer` | 매주 일 04:00 | 오래된 스냅샷 정리 (90일 보존) |

## 파일 구성

| 파일 | 역할 |
|------|------|
| `main.py` | consumer 데몬: Kafka 수동 커밋, 배치 pipeline, Prometheus, 장애 복구 |
| `consumer.py` | EventProcessor: parse_event (파싱), flush_batch (Redis pipeline) |
| `backup.py` | 증분 백업: 분산 락, RENAME, HSCAN, ThreadPool, restic 재시도 |
| `prune.py` | 스냅샷 정리: 분산 락, MinIO SDK repo 탐색, restic forget |
| `cli.py` | CLI: status, snapshots, restore |
| `config.toml` | 설정 (시크릿 미포함) |

## 의존성

- Python 3.9+
- Redis 7+
- MinIO (S3)
- restic 0.16+

```bash
pip install -r requirements.txt
# confluent-kafka, redis, toml, minio, prometheus_client
```

## 설정

`/etc/backup-consumer/config.toml`:

```toml
[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
restic_binary = "/usr/bin/restic"
workers = 4                     # repo 병렬 worker 수
base_path = "/home"
repo_depth = 2                  # /home/{stor}/{user} → repo

[minio]
endpoint = "minio:9000"
bucket = "file-tracker-backup"
use_tls = false

[db]
redis_url = "redis://localhost:6379/0"

[prune]
keep_days = 90

[logging]
level = "INFO"                  # DEBUG, INFO, WARNING, ERROR

[metrics]
port = 9101                     # Prometheus HTTP 포트
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

## 스케일링

Kafka 파티션 10개, consumer group 기반 수평 확장:

```
consumer 1대 → 파티션 10개 전부 담당
consumer 2대 → 각 5개씩 (자동 리밸런싱)
consumer 5대 → 각 2개씩
consumer 10대 → 각 1개씩 (최대)
```

같은 `group_id`, 다른 `[metrics] port`로 프로세스 추가:

```bash
# 노드 A
sudo systemctl start backup-consumer  # port 9101

# 노드 B (같은 group_id)
sudo systemctl start backup-consumer  # port 9101 (별도 노드)
```

## 모니터링

### Prometheus 메트릭 (`:9101/metrics`)

| 메트릭 | 타입 | 설명 |
|--------|------|------|
| `backup_events_processed_total` | counter | 처리된 이벤트 |
| `backup_events_skipped_delete_total` | counter | 무시된 delete |
| `backup_events_errors_total` | counter | 처리 에러 |
| `backup_events_redis_errors_total` | counter | Redis pipeline 실패 |
| `backup_pending_total` | gauge | Redis pending 건수 |
| `backup_last_run_timestamp` | gauge | 마지막 백업 시각 |
| `backup_last_run_backed_up` | gauge | 마지막 백업 건수 |
| `backup_last_run_errors` | gauge | 마지막 백업 에러 |
| `backup_last_run_duration_seconds` | gauge | 마지막 백업 소요시간 |
| `backup_kafka_errors_total{type}` | counter | Kafka 에러 (유형별) |
| `backup_kafka_commit_errors_total` | counter | Kafka commit 실패 |

### Prometheus scrape 설정

```yaml
scrape_configs:
  - job_name: backup-consumer
    static_configs:
      - targets: ["consumer-host:9101"]
```

### 알림 예시

```yaml
groups:
  - name: backup
    rules:
      - alert: BackupPendingHigh
        expr: backup_pending_total > 100000
        for: 1h
      - alert: BackupFailed
        expr: backup_last_run_errors > 0
      - alert: KafkaAllBrokersDown
        expr: increase(backup_kafka_errors_total{type="all_brokers_down"}[5m]) > 0
```

## 장애 복구

| 시나리오 | 처리 |
|---------|------|
| Kafka 시작 시 다운 | 5초 간격 무한 재시도 (list_topics 확인) |
| Kafka 운영 중 다운 | librdkafka 자동 재연결 + error_cb 메트릭 |
| Kafka commit 실패 | 다음 배치에서 자연 재커밋 |
| Redis 시작 시 다운 | 무한 재시도 (consumer) / 3회 재시도 (backup) |
| Redis 운영 중 다운 | flush 실패 → commit 안 함 → 복구 후 Kafka 재수신 |
| backup 프로세스 kill | processing 키 잔존 → 다음 실행에서 이어서 처리 |
| backup 동시 실행 | Redis 분산 락 (SETNX, TTL 2h) |
| restic 실패 | 3회 재시도 (10s, 20s) → 실패 시 pending에 복원 |

## 상태 확인

```bash
# 서비스 상태
systemctl status backup-consumer
systemctl list-timers backup-*

# 로그
journalctl -u backup-consumer -f
journalctl -u backup-run -f
journalctl -u backup-prune -f

# Prometheus 메트릭
curl http://localhost:9101/metrics | grep backup_
```

## CLI

```bash
# 상태 (pending 건수, repo 목록)
python3 cli.py status
python3 cli.py status -v    # 이벤트 유형별 카운트 + 각 repo 마지막 스냅샷

# 스냅샷 조회 (경로로 repo 자동 결정)
python3 cli.py snapshots --path /home/stor1/userA/docs/a.txt
python3 cli.py snapshots --repo stor1/userA
python3 cli.py snapshots --repo stor1/userA --json

# 복원
python3 cli.py restore /home/stor1/userA/docs/a.txt
python3 cli.py restore /home/stor1/userA/docs/a.txt -s abc123 -t /restore
```

## repo 구조

경로의 depth=2로 자동 분리:

| 파일 경로 | repo_id |
|-----------|---------|
| `/home/stor1/userA/docs/a.txt` | `stor1/userA` |
| `/home/stor1/userB/data/b.txt` | `stor1/userB` |
| `/home/stor2/deptC/proj/c.txt` | `stor2/deptC` |

MinIO:
```
s3://file-tracker-backup/
  stor1/userA/ ← restic repo
  stor1/userB/ ← restic repo
  stor2/deptC/ ← restic repo
```

## 수동 실행

```bash
# 환경 변수 설정
export RESTIC_PASSWORD="..."
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin

# 수동 백업
python3 backup.py config.toml

# 수동 prune
python3 prune.py config.toml

# restic 직접 사용
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA snapshots
restic -r s3:http://minio:9000/file-tracker-backup/stor1/userA restore latest --target /restore
```

## 설계 결정

| 결정 | 이유 |
|------|------|
| **수동 Kafka 커밋** | Redis 쓰기 성공 후에만 offset 커밋 → at-least-once 보장 |
| **배치 pipeline (100건/5초)** | 건건 HSET 대비 Redis RTT 절감, commit 빈도 감소 |
| **delete 이벤트 무시** | 삭제 파일은 이전 스냅샷에 보존. 보존기간 후 prune 정리 |
| **ts 비교 없음** | pending은 "확인 필요" 마커. Lustre 현재 상태 직접 읽음 |
| **RENAME 배치 처리** | consumer와 backup 간 레이스 제거 |
| **HSCAN 1만 건** | Redis 단일 스레드 블로킹 방지 |
| **Redis 분산 락** | 멀티노드 backup/prune 동시 실행 방지 |
| **시크릿 분리** | EnvironmentFile로 config.toml에서 비밀번호 제거 |
| **systemd timer** | OS 레벨 스케줄, Persistent=true로 누락 실행 보상 |
| **minio Python SDK** | mc CLI 외부 의존성 제거 |
| **restic 3회 재시도** | 일시적 MinIO/네트워크 장애 대응, 실패 시 pending 복원 |
