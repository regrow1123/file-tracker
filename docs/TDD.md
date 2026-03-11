# TDD: 파일 변경 추적 및 증분 스냅샷 백업 시스템 기술 설계

## 1. 시스템 아키텍처

```
┌──────────────────────────────────────────────────────────┐
│  노드 (수백 대, Lustre)                                   │
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
                    ┌────▼────┐
                    │  Kafka  │
                    └────┬────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  중앙 서버                                                │
│  ┌──────────────────────────────────────────────────┐    │
│  │  backup-consumer (소비자)                          │    │
│  │                                                   │    │
│  │  Kafka consumer → 변경 DB (중복 제거)              │    │
│  │                        │                          │    │
│  │  백업 트리거 (cron/수동)                            │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  노드에서 파일 pull (SSH/rsync)                     │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  restic backup → MinIO (S3)                       │    │
│  │       │                                           │    │
│  │  offset commit + 변경 DB 정리                      │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────────┐                                    │
│  │  MinIO (S3)      │ ← restic 리포지토리               │
│  │  청크 dedup      │ ← 버전별 스냅샷                    │
│  └──────────────────┘                                    │
└──────────────────────────────────────────────────────────┘
```

---

# Part A: 에이전트 (file-tracker) — 구현 완료

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
                          └─ 실패 → WAL 기록
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

# Part B: 소비자 (backup-consumer) — 설계

## 4. 소비자 아키텍처

### 4.1 컴포넌트

```
┌─────────────────────────────────────────────┐
│  backup-consumer                             │
│                                              │
│  ┌─────────────┐   ┌──────────────────────┐ │
│  │ Kafka       │──▶│ Event Processor      │ │
│  │ Consumer    │   │ - 이벤트 파싱         │ │
│  └─────────────┘   │ - 변경 DB upsert     │ │
│                     └──────────────────────┘ │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ Backup Scheduler                        │ │
│  │ - cron 또는 수동 트리거                   │ │
│  │ - 노드별 변경 파일 목록 생성              │ │
│  │ - 노드에서 파일 pull                     │ │
│  │ - restic backup 실행                    │ │
│  │ - 완료 시 변경 DB 정리 + offset commit   │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  ┌─────────────┐   ┌──────────────────────┐ │
│  │ API Server  │   │ 변경 DB              │ │
│  │ (선택)      │   │ (Redis/PostgreSQL)   │ │
│  └─────────────┘   └──────────────────────┘ │
└──────────────────────────────────────────────┘
```

### 4.2 변경 DB 스키마

```sql
CREATE TABLE changed_files (
    hostname    TEXT NOT NULL,
    path        TEXT NOT NULL,
    event       TEXT NOT NULL,   -- mtime_change, delete, rename
    old_path    TEXT,            -- rename인 경우
    event_ts    BIGINT NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (hostname, path)
);

-- upsert: 같은 (hostname, path)에 대해 최신 이벤트만 유지
```

### 4.3 이벤트 처리 규칙

| Kafka 이벤트 | 변경 DB 동작 |
|-------------|-------------|
| `mtime_change` path=A | UPSERT (host, A) → mtime_change |
| `delete` path=A | UPSERT (host, A) → delete |
| `rename` old=A new=B | DELETE (host, A) + UPSERT (host, B) → rename |

### 4.4 백업 실행 흐름

```
1. 변경 DB에서 hostname별 변경 파일 목록 추출
2. 노드별로:
   a. mtime_change 파일 목록 → /tmp/backup_list_{hostname}.txt
   b. SSH로 노드 접속, restic backup 실행:
      restic backup \
        --files-from /tmp/backup_list_{hostname}.txt \
        --repo s3:http://minio:9000/backup-{hostname} \
        --tag incremental
   c. delete 파일 → 별도 기록 (restic은 자동 관리)
   d. rename → old_path는 무시, new_path만 백업
3. 백업 완료 → 변경 DB에서 처리된 항목 제거
4. Kafka offset commit
```

### 4.5 restic 리포지토리 구조

```
MinIO:
  s3://backup-node001/    ← node-001의 restic 리포지토리
  s3://backup-node002/    ← node-002의 restic 리포지토리
  ...

각 리포지토리 내부 (restic 관리):
  /config
  /data/       ← 청크 dedup된 데이터
  /index/
  /keys/
  /locks/
  /snapshots/  ← 시점별 스냅샷 메타데이터
```

### 4.6 스냅샷 관리

```bash
# 스냅샷 목록
restic snapshots -r s3:http://minio:9000/backup-node001

# 특정 시점 파일 복원
restic restore <snapshot-id> \
  --target /restore \
  --include /home/user/file.txt \
  -r s3:http://minio:9000/backup-node001

# 오래된 스냅샷 정리 (90일 보존)
restic forget --keep-within 90d --prune \
  -r s3:http://minio:9000/backup-node001
```

### 4.7 풀스캔 보정

이벤트 유실 대비 주기적 풀스캔:

```
매주 일요일 새벽:
  1. 각 노드에서 find /home -type f 실행
  2. 결과를 현재 restic 스냅샷과 비교
  3. 차이 있는 파일만 백업
```

## 5. 기술 선택지

### 5.1 소비자 언어

| 선택지 | 장점 | 단점 |
|--------|------|------|
| Python | 빠른 개발, kafka-python/confluent-kafka | 성능 (우리 규모에선 충분) |
| Go | 단일 바이너리, sarama/confluent-kafka-go | |
| Java | Kafka 네이티브 | 무거움 |

**추천: Python** — 소비자는 I/O 바운드(Kafka read + SSH + restic 호출), CPU 집약 아님.

### 5.2 변경 DB

| 선택지 | 장점 | 단점 |
|--------|------|------|
| Redis | 빠름, 간단 | 영속성 설정 필요 |
| PostgreSQL | 쿼리 강력, 트랜잭션 | 무거움 |
| SQLite | 단일 파일, 무설치 | 단일 프로세스 |

**추천: Redis** — upsert가 핵심 연산, 빠르고 간단. 소비자 1대면 SQLite도 가능.

## 6. 설정

### 6.1 소비자 설정

```toml
# /etc/backup-consumer/config.toml

[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
schedule = "0 3 * * *"           # 매일 새벽 3시
restic_binary = "/usr/bin/restic"
minio_endpoint = "http://minio:9000"
minio_access_key = "minioadmin"
minio_secret_key = "minioadmin"
bucket_prefix = "backup-"        # backup-{hostname}

[db]
type = "redis"                   # redis 또는 sqlite
redis_url = "redis://localhost:6379/0"

[nodes]
ssh_user = "backup"
ssh_key = "/etc/backup-consumer/id_rsa"
# 노드 목록은 Kafka 이벤트의 hostname에서 자동 수집

[fullscan]
enabled = true
schedule = "0 2 * 0"             # 매주 일요일 새벽 2시
```

## 7. 구현 단계

| Phase | 내용 | 의존성 | 예상 공수 |
|-------|------|--------|----------|
| **B-1** | 소비자 기본: Kafka consumer + 변경 DB (Redis) | Kafka, Redis | 1일 |
| **B-2** | restic + MinIO 연동: 단일 노드 증분 백업 | MinIO, restic | 1일 |
| **B-3** | 멀티 노드: SSH pull + 노드별 restic 리포지토리 | SSH 접근 | 1일 |
| **B-4** | 스케줄러: cron 백업 + 스냅샷 정리 (forget/prune) | | 반나절 |
| **B-5** | 풀스캔 보정: 주기적 전체 비교 + 차분 백업 | | 반나절 |
| **B-6** | CLI/API: 스냅샷 조회, 파일 복원, 상태 확인 | | 1일 |
| **B-7** | 운영: systemd, 로깅, 모니터링, 문서 | | 반나절 |

## 8. 알려진 제약

| 제약 | 영향 | 대응 |
|------|------|------|
| 이벤트~pull 사이 파일 재변경 | 중간 버전 누락 가능 | 다음 이벤트에서 재백업 (최종 일관성) |
| Kafka retention 초과 | 이벤트 유실 | 주기적 풀스캔 보정 |
| 대용량 파일 pull 시 네트워크 부하 | 백업 시간 증가 | restic 청크 dedup으로 변경분만 전송 |
| 삭제된 파일은 pull 불가 | 삭제 전 버전만 보존 | 이전 스냅샷에서 복원 가능 |
| 노드 접근 불가 (다운) | 해당 노드 백업 건너뜀 | 다음 주기에 재시도, 변경 DB 유지 |
