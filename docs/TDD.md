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
                    │  Kafka  │
                    └────┬────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  중앙 서버 (Lustre 마운트)                                 │
│  ┌──────────────────────────────────────────────────┐    │
│  │  backup-consumer (소비자)                          │    │
│  │                                                   │    │
│  │  Kafka consumer → Redis (경로 기준 중복 제거)       │    │
│  │                        │                          │    │
│  │  백업 트리거 (cron/수동)                            │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  Redis에서 변경 목록 → N worker 분배               │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  kopia snapshot (병렬) → MinIO (S3)               │    │
│  │       │                                           │    │
│  │  Redis 정리 + Kafka offset commit                 │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────────┐                                    │
│  │  MinIO (S3)      │ ← 단일 kopia 리포지토리            │
│  │  청크 dedup      │ ← 시점별 스냅샷                    │
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

### 4.1 환경 전제

- **Lustre 공유 마운트**: 모든 노드와 소비자가 동일한 `/home`을 공유
- 동일 파일에 대해 여러 노드에서 이벤트 발생 가능 → **경로 기준** 중복 제거
- 소비자가 Lustre를 직접 마운트 → SSH pull 불필요, 로컬 파일 접근

### 4.2 컴포넌트

```
┌──────────────────────────────────────────────────────┐
│  backup-consumer                                      │
│                                                       │
│  ┌─────────────┐   ┌──────────────────────────────┐  │
│  │ Kafka       │──▶│ Event Processor               │  │
│  │ Consumer    │   │ - 이벤트 파싱                   │  │
│  └─────────────┘   │ - 경로 기준 Redis upsert       │  │
│                     │ - hostname 무시 (Lustre 공유)   │  │
│                     └──────────────────────────────┘  │
│                                                       │
│  ┌──────────────────────────────────────────────────┐ │
│  │ Backup Scheduler                                  │ │
│  │ - cron 또는 수동 트리거                             │ │
│  │ - Redis에서 변경 파일 목록 추출                      │ │
│  │ - N개 worker에 배치 분배                           │ │
│  │ - 각 worker: staging dir → kopia snapshot (병렬)   │ │
│  │ - 완료 시 Redis 정리 + Kafka offset commit        │ │
│  └──────────────────────────────────────────────────┘ │
│                                                       │
│  ┌────────────────┐  ┌──────────┐                    │
│  │ Backup Workers │  │  Redis   │                    │
│  │ (N개, 병렬)    │  │ 변경 DB  │                    │
│  └───────┬────────┘  └──────────┘                    │
│          │                                            │
│          ▼                                            │
│  ┌────────────────┐                                   │
│  │ kopia repo     │→ MinIO (S3)                       │
│  │ (단일, 공유)   │   동시 쓰기 지원                    │
│  └────────────────┘                                   │
└──────────────────────────────────────────────────────┘
```

### 4.3 변경 DB (Redis)

**Hash 하나로 관리:**

```
HSET pending "/home/user/a.txt" '{"event":"mtime_change","ts":1710000000}'
HSET pending "/home/user/b.txt" '{"event":"delete","ts":1710000100}'
HSET pending "/home/user/c.txt" '{"event":"rename","old_path":"/home/user/old.txt","ts":1710000200}'
```

- `HSET`: 같은 경로면 덮어쓰기 (upsert, 중복 제거)
- `HLEN pending`: 대기 건수
- `HGETALL pending`: 전체 조회 (배치 분배 시)
- `HDEL pending <path>`: 백업 완료 후 제거

### 4.4 이벤트 처리 규칙

| Kafka 이벤트 | Redis 동작 |
|-------------|-----------|
| `mtime_change` path=A | `HSET pending A '{"event":"mtime_change"}'` |
| `delete` path=A | `HSET pending A '{"event":"delete"}'` |
| `rename` old=A new=B | `HDEL pending A` + `HSET pending B '{"event":"rename","old_path":"A"}'` |

### 4.5 백업 실행 흐름 (병렬)

kopia는 `--file-list` 옵션이 없다. 디렉토리를 스냅샷하는 방식이므로 **staging directory**를 사용:

```
1. 백업 트리거 (cron 또는 수동)
2. Redis에서 변경 파일 목록 추출:
   - HGETALL pending → 전체 {path: info} 맵
   - N건을 W개 worker에 균등 분배
3. 각 worker 병렬 실행:
   a. staging dir 생성 (/tmp/backup-staging-{worker_id}/)
   b. 변경 파일의 디렉토리 구조를 staging에 재현 (symlink):
      ln -s /home/user/a.txt /tmp/backup-staging-0/home/user/a.txt
   c. kopia snapshot create /tmp/backup-staging-{worker_id} \
        --tags type:incremental,worker:{worker_id}
   d. staging dir 삭제
   e. 처리된 항목 HDEL pending <path>
4. 모든 worker 완료
5. Kafka offset commit
```

**symlink 사용 이유:**
- Lustre와 로컬 /tmp은 다른 파일시스템 → hardlink 불가
- symlink는 kopia가 기본적으로 따라감 (follow symlinks)
- 실제 데이터 복사 없음 → 빠름

**delete 이벤트 처리:**
- 삭제된 파일은 Lustre에 이미 없으므로 백업 불가
- 이전 스냅샷에 해당 파일이 보존되어 있음 → 복원 가능
- delete 이벤트는 Redis에서 기록만 하고 staging에 포함하지 않음

### 4.6 kopia 리포지토리

단일 리포지토리, 동시 쓰기 지원:

```bash
# 리포지토리 생성 (최초 1회)
kopia repository create s3 \
  --bucket file-tracker-backup \
  --endpoint minio:9000 \
  --disable-tls \
  --access-key minioadmin \
  --secret-access-key minioadmin

# 리포지토리 연결 (각 worker 노드에서)
kopia repository connect s3 \
  --bucket file-tracker-backup \
  --endpoint minio:9000 \
  --disable-tls

# 증분 백업 (worker별 병렬 실행 가능)
kopia snapshot create /tmp/backup-staging-0 \
  --tags type:incremental,worker:0

# kopia는 content-addressed storage:
# 같은 데이터는 한 번만 저장 (cross-snapshot dedup)
```

```
MinIO:
  s3://file-tracker-backup/    ← 단일 kopia 리포지토리
    /kopia.repository          ← 리포지토리 메타
    /p.../                     ← 청크 데이터 (content-addressed)
    /q.../                     ← 인덱스
```

### 4.7 스냅샷 관리

```bash
# 스냅샷 목록
kopia snapshot list --all

# 특정 태그의 스냅샷만
kopia snapshot list --tags type:incremental

# 시점 복원 (전체)
kopia restore <snapshot-id> /restore/

# 특정 파일만 복원
kopia restore <snapshot-id>/home/user/file.txt /restore/file.txt

# 오래된 스냅샷 정리 (90일 보존)
kopia policy set --global --keep-daily 90
kopia snapshot expire
kopia maintenance run --full
```

### 4.8 풀스캔 보정

이벤트 유실 대비 주기적 풀스캔:

```
매주 일요일 새벽:
  1. Lustre에서 find /home -type f -newer <last_fullscan_marker> 실행
  2. 결과를 Redis pending_files에 추가
  3. 정상 백업 흐름으로 처리
```

## 5. 기술 선택

### 5.1 소비자 언어: Python

I/O 바운드 (Kafka read + kopia 호출). CPU 집약 아님. confluent-kafka-python + redis-py.

### 5.2 백업 도구: kopia

| 비교 | restic | kopia |
|------|--------|-------|
| 동시 쓰기 | 불가 (리포지토리 락) | **가능** |
| S3 지원 | O | O |
| 청크 dedup | O | O |
| 병렬 처리 | 제한적 | 강력 (멀티스레드) |
| 파일 목록 백업 | `--files-from` | staging dir 필요 |
| 라이선스 | BSD 2-Clause | Apache 2.0 |

Lustre 공유 환경에서 다수 worker 병렬 백업 → kopia.
restic은 `--files-from`이 편리하지만 동시 쓰기 불가가 치명적.

### 5.3 변경 DB: Redis

| 비교 | Redis | PostgreSQL | SQLite |
|------|-------|-----------|--------|
| 동시 접근 | O | O | X |
| upsert 성능 | 최고 | 중간 | 느림 |
| 배치 분배 (HGETALL) | O | SELECT FOR UPDATE | X |
| 영속성 | AOF/RDB | 기본 | 기본 |

## 6. 설정

```toml
# /etc/backup-consumer/config.toml

[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
schedule = "0 3 * * *"              # 매일 새벽 3시
kopia_binary = "/usr/bin/kopia"
workers = 10                        # 병렬 backup worker 수
staging_dir = "/tmp/backup-staging"  # staging 기본 경로
lustre_mount = "/home"              # Lustre 마운트 포인트

[minio]
endpoint = "minio:9000"
bucket = "file-tracker-backup"
access_key = "minioadmin"
secret_key = "minioadmin"
use_tls = false

[db]
redis_url = "redis://localhost:6379/0"

[fullscan]
enabled = true
schedule = "0 2 * * 0"              # 매주 일요일 새벽 2시
```

## 7. 구현 단계

| Phase | 내용 | 의존성 | 예상 공수 |
|-------|------|--------|----------|
| **B-1** | Kafka consumer + Redis 변경 DB | Kafka, Redis | 1일 |
| **B-2** | kopia + MinIO 연동: staging dir 방식 단일 worker 백업 | MinIO, kopia | 1일 |
| **B-3** | 병렬 백업: N worker 동시 kopia snapshot | | 1일 |
| **B-4** | 스케줄러: cron 백업 + 스냅샷 정리 | | 반나절 |
| **B-5** | 풀스캔 보정: 주기적 전체 비교 + 차분 백업 | | 반나절 |
| **B-6** | CLI: 스냅샷 조회, 파일 복원, 상태 확인 | | 1일 |
| **B-7** | 운영: systemd, 로깅, 모니터링, 문서 | | 반나절 |

## 8. 알려진 제약

| 제약 | 영향 | 대응 |
|------|------|------|
| 이벤트~백업 사이 파일 재변경 | 중간 버전 누락 가능 | 다음 이벤트에서 재백업 (최종 일관성) |
| Kafka retention 초과 | 이벤트 유실 | 주기적 풀스캔 보정 |
| 대용량 파일 백업 시 네트워크 부하 | 백업 시간 증가 | kopia 청크 dedup으로 변경분만 전송 |
| 삭제된 파일은 Lustre에 없음 | 삭제 전 버전만 보존 | 이전 스냅샷에서 복원 가능 |
| 같은 파일에 대한 다중 노드 이벤트 | 중복 이벤트 | Redis 경로 기준 upsert로 중복 제거 |
| kopia에 --file-list 없음 | 개별 파일 선택 백업 불가 | staging dir + symlink로 해결 |
| symlink 대상 파일 삭제 시 | staging에 깨진 symlink | delete 이벤트는 staging 제외 |
