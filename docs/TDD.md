# TDD: 파일 변경 추적 및 증분 스냅샷 백업 시스템 기술 설계

## 1. 시스템 아키텍처

```
┌──────────────────────────────────────────────────────────┐
│  노드 (수백 대)                                           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  file-tracker (에이전트)                           │    │
│  │                                                   │    │
│  │  Kernel:                                          │    │
│  │   kprobe(5종) → inode dedup(LRU) → ring buffer    │    │
│  │                                                   │    │
│  │  Userspace:                                       │    │
│  │   path 조립 → /home 필터 → debounce → Kafka       │    │
│  │                                    └→ WAL(실패시)  │    │
│  └──────────────────────────────────────────────────┘    │
└────────────────────────┬─────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │  Lustre (/home)     │ ← 모든 노드 + 소비자 공유 마운트
              └──────────┬──────────┘
                         │
                    ┌────▼────┐
                    │  Kafka  │  10 파티션, 파티션 키=hostname
                    └────┬────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  소비자 (Lustre 마운트, 멀티노드 스케일 가능)              │
│  ┌──────────────────────────────────────────────────┐    │
│  │  backup-consumer                                  │    │
│  │                                                   │    │
│  │  Kafka consumer (수동 커밋, at-least-once)         │    │
│  │   → 배치 파싱 (100건/5초)                          │    │
│  │   → Redis pipeline flush                          │    │
│  │   → flush 성공 후 Kafka commit                    │    │
│  │                        │                          │    │
│  │  백업 (systemd timer, 매일 03:00)                  │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  분산 락 → RENAME → HSCAN → repo별 그룹핑          │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  ThreadPoolExecutor → restic --files-from → MinIO │    │
│  │   실패: 3회 재시도 → pending 복원                   │    │
│  │                                                   │    │
│  │  Prometheus :9101/metrics                         │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────────┐   ┌──────────────────┐            │
│  │  Redis (AOF)     │   │  MinIO (S3)      │            │
│  │  pending Hash    │   │  단일 버킷       │            │
│  │  분산 락         │   │  prefix별 repo   │            │
│  └──────────────────┘   └──────────────────┘            │
└──────────────────────────────────────────────────────────┘
```

---

# Part A: 에이전트 (file-tracker)

## 2. eBPF 프로그램

### 2.1 kprobe 대상

| 이벤트 | kprobe | 비고 |
|--------|--------|------|
| 삭제 | `vfs_unlink` | unlink/unlinkat 포괄 |
| 쓰기 | `vfs_write` | write/pwrite64 포괄 |
| 절단 | `do_truncate` | truncate/ftruncate 포괄 |
| 시간변경 | `vfs_utimes` | utimensat/utime 포괄 |
| 이름변경 | `vfs_rename` | rename/renameat2 포괄 |

### 2.2 BPF Maps

| Map | 타입 | 용도 |
|-----|------|------|
| `events` | RINGBUF (16MB) | 이벤트 전달 |
| `scratch` | PERCPU_ARRAY | 이벤트 조립용 임시 버퍼 |
| `counters` | PERCPU_ARRAY (3항목) | drops/total/dedup 카운터 |
| `dedup` | LRU_HASH (100K) | inode 기반 1초 mtime 중복 억제 |

### 2.3 이벤트 구조체

```c
struct file_event {
    u64 ts_ns;
    u32 event_type;     // 0=delete, 1=mtime, 2=rename_from, 3=rename_to
    u32 depth;
    char names[20][256]; // dentry 컴포넌트 배열 (유저스페이스에서 조립)
};
```

### 2.4 CO-RE 호환

- `void *idmap_or_userns`: vfs_unlink/do_truncate 첫 인자 (5.14 vs 6.x)
- `struct renamedata *`: vfs_rename (5.12+ 동일)
- BTF 기반 오프셋 자동 패치
- 단일 바이너리로 RHEL 9 (5.14) + RHEL 10 (6.12) 지원

## 3. 유저스페이스 데몬

### 3.1 기술 스택

| 항목 | 선택 |
|------|------|
| 언어 | C++ (C++20) |
| BPF | libbpf (CO-RE) |
| Kafka | librdkafka (lz4 압축) |
| 설정 | toml++ (header-only) |
| WAL | 커스텀 append-only (CRC32) |

### 3.2 이벤트 처리

```
Ring Buffer Poll (100ms)
    │
    ├─ rename_from → 페어링 대기
    ├─ rename_to → from과 합쳐서 단일 rename JSON 전송
    ├─ delete → debounce bypass, 즉시 전송
    └─ mtime → /home 필터 → debouncer (10s quiet, 1h max)
                                │
                                ▼
                          Kafka send
                          ├─ 성공 → 완료
                          └─ 실패 → WAL 기록 → 30초 재시도
```

### 3.3 성능 (부하 테스트 결과)

| 항목 | 결과 (130만 IOPS) |
|------|-------------------|
| CPU | 0.24% |
| RSS | 43MB |
| ring buffer drops | 0 |
| BPF dedup rate | 99.99% |

### 3.4 배포

- systemd unit: `LimitMEMLOCK=infinity`, `MemoryMax=100M`, `CPUQuota=5%`
- RPM: `make deps && make deps-rdkafka && make rpm`
- 설정: `/etc/file-tracker/config.toml`

---

# Part B: 소비자 (backup-consumer)

## 4. 소비자 아키텍처

### 4.1 환경 전제

- **Lustre 공유 마운트**: 모든 노드와 소비자가 동일한 `/home`을 공유
- 동일 파일에 대해 여러 노드에서 이벤트 발생 가능 → **경로 기준** 중복 제거
- 소비자가 Lustre를 직접 마운트 → SSH pull 불필요
- 디렉토리 구조: `/home/{스토리지명}/{사용자 또는 부서}/...`
- **멀티노드 스케일 가능**: 같은 Kafka group_id로 소비자 추가 시 파티션 자동 리밸런싱

### 4.2 repo 분리 전략

경로의 depth 기준으로 restic repo를 자동 분리:

```python
def get_repo_id(path, base="/home", depth=2):
    rel = path.removeprefix(base + "/")   # "stor1/userA/docs/a.txt"
    parts = rel.split("/")
    return "/".join(parts[:depth])         # "stor1/userA"
```

**예시 (depth=2):**

| 파일 경로 | repo_id |
|-----------|---------|
| `/home/stor1/userA/docs/a.txt` | `stor1/userA` |
| `/home/stor1/userB/data/b.txt` | `stor1/userB` |
| `/home/stor2/deptC/proj/c.txt` | `stor2/deptC` |

**MinIO 구조:**

```
s3://file-tracker-backup/
  stor1/userA/config        ← restic repo
  stor1/userA/data/...
  stor1/userA/snapshots/...
  stor1/userB/config        ← restic repo
  stor2/deptC/config        ← restic repo
  ...
```

단일 버킷, prefix로 repo 분리. repo 수 제한 없음.

### 4.3 서비스 구성

3개의 독립 서비스 + Prometheus:

```
┌─────────────────────────────────────────────────────────┐
│  backup-consumer.service (상시 실행)                      │
│  ┌─────────────┐   ┌──────────────────────────────────┐ │
│  │ Kafka       │──▶│ EventProcessor (배치 모드)         │ │
│  │ Consumer    │   │ - parse_event(): 파싱만, Redis 무  │ │
│  │ (수동 커밋) │   │ - flush_batch(): pipeline 일괄     │ │
│  └─────────────┘   │ - flush 성공 → Kafka commit       │ │
│                     │ - flush 실패 → commit 안 함       │ │
│                     │   → Kafka에서 재수신 (at-least-1) │ │
│                     └──────────────────────────────────┘ │
│                              ↓                           │
│  Prometheus :9101/metrics                                │
│  - backup_events_processed_total                         │
│  - backup_events_skipped_delete_total                    │
│  - backup_events_errors_total                            │
│  - backup_events_redis_errors_total                      │
│  - backup_pending_total                                  │
│  - backup_last_run_{timestamp,backed_up,errors,duration} │
│  - backup_kafka_errors_total{type}                       │
│  - backup_kafka_commit_errors_total                      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  backup-run.timer → backup-run.service (매일 03:00)      │
│  1. 분산 락 획득 (SET backup:lock NX EX 7200)            │
│  2. RENAME pending → processing (원자적 swap)            │
│     - processing 잔존 시 → 중단된 백업 이어서 처리       │
│  3. HSCAN processing (1만 건 배치)                       │
│  4. repo별 그룹핑 (get_repo_id)                          │
│  5. ThreadPoolExecutor → repo별 restic backup            │
│     - 실패: 3회 재시도 (10s, 20s backoff)                │
│     - 최종 실패: paths를 pending에 복원 (value="retry")   │
│  6. DELETE processing                                    │
│  7. 결과 → Redis backup:last_run (Prometheus용)          │
│  8. 분산 락 해제                                         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  backup-prune.timer → backup-prune.service (매주 일 04:00)│
│  1. 분산 락 획득 (backup/prune 동시 실행 방지)            │
│  2. MinIO SDK로 repo 목록 탐색                           │
│  3. 각 repo: restic forget --keep-within=90d --prune     │
│  4. 분산 락 해제                                         │
└─────────────────────────────────────────────────────────┘
```

### 4.4 Kafka 소비 — 수동 커밋 + 배치 파이프라인

```
Kafka poll (1s timeout)
    │
    ├─ 이벤트 수신 → parse_event() (파싱만, Redis 호출 없음)
    │                 │
    │                 └→ 배치 큐에 추가
    │
    ├─ 배치 크기 ≥ 100건
    │   OR 마지막 flush 후 ≥ 5초
    │       │
    │       ▼
    │   Redis pipeline flush
    │       │
    │       ├─ 성공 → kafka_consumer.commit() → 배치 리셋
    │       │
    │       └─ 실패 → commit 안 함 → redis_down 상태 진입
    │                  → ping 재연결 루프 (Kafka poll은 유지)
    │                  → 복구 후 Kafka에서 미커밋 이벤트 재수신
    │
    └─ HSET은 idempotent → 중복 처리 무해
```

**at-least-once 보장**: Redis에 쓰기 성공한 후에만 Kafka offset을 커밋. Redis 장애 시 offset이 올라가지 않으므로 복구 후 같은 이벤트를 다시 받는다.

### 4.5 Kafka 장애 핸들링

| 시나리오 | 처리 |
|---------|------|
| 시작 시 브로커 다운 | `list_topics()` 연결 확인 → 5초 간격 무한 재시도 |
| 운영 중 브로커 다운 | librdkafka 자동 재연결 + `error_cb` 콜백 로깅/메트릭 |
| commit 실패 | `safe_commit()` catch → 로그 + 다음 배치에서 자연 재커밋 |

```python
def error_cb(err):
    if err.code() == KafkaError._ALL_BROKERS_DOWN:
        log.error("Kafka: 모든 브로커 다운")
    elif err.code() == KafkaError._TRANSPORT:
        log.warning("Kafka: 브로커 연결 끊김 (자동 재연결)")
```

librdkafka 통계: 60초 간격 `stats_cb`로 DEBUG 레벨 로깅.

### 4.6 Redis 장애 핸들링

| 시나리오 | 처리 |
|---------|------|
| consumer 시작 시 Redis 다운 | `connect_redis()` 5초 간격 무한 재시도 |
| consumer 운영 중 Redis 다운 | flush 실패 → commit 안 함 → ping 재연결 → Kafka 재수신 |
| backup 시작 시 Redis 다운 | 3회 재시도 (10s, 20s 간격) → exit 1 (systemd 재스케줄) |
| backup 중단 (kill/OOM) | processing 키 잔존 → 다음 실행에서 이어서 처리 |

### 4.7 변경 DB (Redis)

```
HSET pending "/home/stor1/userA/docs/a.txt" "mtime_change"
HSET pending "/home/stor2/deptC/proj/c.txt" "rename"
```

- **단순 문자열 값**: event_type만 저장 (ts, JSON 불필요)
- `HSET`: 같은 경로면 덮어쓰기 (자연 dedup)
- `HLEN pending`: 대기 건수
- `HSCAN processing`: 배치 조회 (1만 건씩, Redis 블로킹 방지)

### 4.8 이벤트 처리 규칙

| Kafka 이벤트 | Redis 동작 |
|-------------|-----------|
| `mtime_change` path=A | `HSET pending A "mtime_change"` |
| `delete` path=A | 무시 (이전 스냅샷에 보존) |
| `rename` old=A new=B | pipeline: `HDEL pending A` + `HSET pending B "rename"` |

**ts 비교 불필요**: pending은 "이 파일을 확인하라"는 마커. 백업 시 Lustre에서 현재 파일을 직접 읽으므로 이벤트 순서가 결과에 영향 없음.

**delete 무시 이유**: 삭제된 파일은 이전 스냅샷에 이미 보존. 보존기간(keep_days) 후 prune에서 자동 정리.

### 4.9 분산 락 — 동시 실행 방지

backup과 prune이 같은 Redis 락을 공유:

```python
# 획득
r.set("backup:lock", f"{hostname}", nx=True, ex=7200)
# 해제
r.delete("backup:lock")
```

| 시나리오 | 결과 |
|---------|------|
| 노드A backup + 노드B backup | B 스킵 (락 충돌) |
| backup + prune 동시 | prune 스킵 |
| 프로세스 kill | TTL 2시간 후 자동 해제 |
| 정상 완료 | finally에서 즉시 해제 |

- `NX`: 키 없을 때만 생성
- `EX 7200`: TTL 2시간 (`TimeoutStartSec=7200`과 일치)
- value에 `hostname(:prune)` 저장 → 어느 노드가 실행 중인지 확인 가능

### 4.10 restic 백업 실행

```python
def backup_repo(repo_id, paths, cfg, env, repo_mgr):
    repo_url = f"s3:{scheme}://{endpoint}/{bucket}/{repo_id}"
    repo_mgr.ensure(repo_url, env)           # init if needed (thread-safe)
    existing = [p for p in paths if os.path.exists(p)]  # 삭제된 파일 제외

    with tempfile.NamedTemporaryFile(mode="w") as f:
        f.write("\n".join(existing))
        # 최대 3회 재시도 (10s, 20s backoff)
        restic backup --files-from f.name -r repo_url --tag incremental
```

- **RepoManager**: `threading.Lock` + `_pending` set으로 같은 repo 중복 init 방지
- **재시도**: 3회, 최종 실패 시 해당 repo의 paths를 `pending`에 `"retry"` 값으로 복원
- **returncode 3**: "일부 파일 누락"은 성공 처리 (WARNING 로그)

### 4.11 Prometheus 모니터링

consumer(main.py)가 상시 실행하며 HTTP `:9101/metrics` 노출:

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

backup 결과는 Redis `backup:last_run` Hash에 기록, consumer가 10초 간격으로 gauge에 반영.

### 4.12 스냅샷 관리

```bash
# 특정 사용자의 스냅샷 목록
restic snapshots -r s3:http://minio:9000/file-tracker-backup/stor1/userA

# 시점 복원
restic restore latest \
  --target /restore \
  --include /home/stor1/userA/docs/a.txt \
  -r s3:http://minio:9000/file-tracker-backup/stor1/userA

# CLI로 복원
python3 cli.py restore /home/stor1/userA/docs/a.txt -t /restore
```

## 5. 기술 선택

### 5.1 소비자 언어: Python

I/O 바운드 (Kafka read + restic 호출). CPU 집약 아님. confluent-kafka-python + redis-py.

### 5.2 백업 도구: restic

| 항목 | restic |
|------|--------|
| `--files-from` | 네이티브 지원 |
| S3 지원 | O |
| 청크 dedup | O (content-defined chunking) |
| 동시 쓰기 | 같은 repo 불가, 다른 repo 가능 |
| 라이선스 | BSD 2-Clause (상업 무료) |

repo를 depth 기반으로 분리하여 동시 쓰기 문제 해결.

### 5.3 변경 DB: Redis

- 단일 Hash (`pending`) 하나로 관리
- `HSET`으로 upsert (자연 중복 제거)
- `HSCAN`으로 배치 조회 (1만 건씩)
- `RENAME`으로 consumer/backup 간 격리
- 분산 락 (`backup:lock`) 으로 멀티노드 동시 실행 방지

## 6. 설정

### 6.1 에이전트 (`/etc/file-tracker/config.toml`)

```toml
[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"

[debounce]
quiet_ms = 10000
max_wait_ms = 3600000

[wal]
path = "/var/lib/file-tracker/wal.log"
max_size_mb = 1024

[watch]
prefix = "/home"

[logging]
level = "info"
```

### 6.2 소비자 (`/etc/backup-consumer/config.toml`)

```toml
[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
restic_binary = "/usr/bin/restic"
workers = 10
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

[metrics]
port = 9101
```

### 6.3 시크릿 (`/etc/backup-consumer/env`, chmod 600)

```
RESTIC_PASSWORD=your-password
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
```

## 7. 테스트

### 7.1 E2E 테스트 (happy path)

`test/e2e.sh` — 24개 체크포인트:

```
PREREQ  → 바이너리, restic, redis, minio SDK, Kafka, MinIO
AGENT   → eBPF 시작, 파일 조작 (생성/수정/삭제/rename), Kafka 전송 확인
CONSUMER → pending 수집, mtime/rename/delete 검증
BACKUP  → 실행, pending 정리, last_run 기록, restic 스냅샷
RESTORE → 실행, 파일 내용 diff 일치
PRUNE   → 실행, 최근 스냅샷 유지 확인
METRICS → Prometheus 메트릭 노출 확인
```

### 7.2 E2E 엣지케이스 테스트

`test/e2e-edge.sh` — 9개 시나리오, 19개 체크포인트:

| 시나리오 | 검증 항목 |
|---------|----------|
| 빈 pending backup | no-op 정상 종료 |
| 존재하지 않는 파일 backup | skipped 처리 |
| 분산 락 충돌 | 스킵 + pending 유지 |
| processing 복구 | 중단 지점부터 이어서 |
| Redis 다운 + 복구 | at-least-once 재수신 |
| rename 체인 A→B→C | 최종 C만 pending |
| 1만건 배치 커밋 | lag 0, 전부 처리 |
| delete 필터링 | 100건 무시, 10건만 저장 |
| WAL 복구 (Kafka 다운) | 복구 후 재전송 |

## 8. 알려진 제약

| 제약 | 영향 | 대응 |
|------|------|------|
| 이벤트~백업 사이 파일 재변경 | 중간 버전 누락 가능 | 다음 이벤트에서 재백업 (최종 일관성) |
| Kafka retention 초과 | 이벤트 유실 | 주기적 풀스캔 보정 |
| 대용량 파일 백업 시 네트워크 부하 | 백업 시간 증가 | restic 청크 dedup으로 변경분만 전송 |
| 삭제된 파일은 Lustre에 없음 | 삭제 전 버전만 보존 | 이전 스냅샷에서 복원 가능 |
| 같은 파일에 대한 다중 노드 이벤트 | 중복 이벤트 | Redis 경로 기준 upsert로 중복 제거 |
| restic repo 분리 후 병합 불가 | depth 변경 시 재백업 필요 | 초기 depth 설계 중요 |
| 같은 repo 동시 쓰기 불가 | 같은 사용자 파일은 순차 | repo 단위 태스크 분배로 해결 |
