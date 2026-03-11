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
│  │  Redis → repo별 그룹핑 → 태스크 큐                 │    │
│  │       │                                           │    │
│  │       ▼                                           │    │
│  │  Worker Pool (N개)                                │    │
│  │   각 worker: restic --files-from → MinIO          │    │
│  │   서로 다른 repo는 동시 실행                        │    │
│  │       │                                           │    │
│  │  Redis 정리 + Kafka offset commit                 │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────────┐                                    │
│  │  MinIO (S3)      │ ← 단일 버킷, prefix별 restic repo  │
│  │  청크 dedup      │                                    │
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
- 소비자가 Lustre를 직접 마운트 → SSH pull 불필요
- 디렉토리 구조: `/home/{스토리지명}/{사용자 또는 부서}/...`

### 4.2 repo 분리 전략

경로의 depth 기준으로 restic repo를 자동 분리:

```python
def get_repo_id(path, base="/home", depth=2):
    rel = path.removeprefix(base + "/")   # "stor1/userA/docs/a.txt"
    parts = rel.split("/")
    return "/".join(parts[:depth])         # "stor1/userA"
```

**예시 (depth=2):**

```
/home/stor1/userA/docs/a.txt  → repo: stor1/userA
/home/stor1/userB/data/b.txt  → repo: stor1/userB
/home/stor2/deptC/proj/c.txt  → repo: stor2/deptC
```

**MinIO 구조:**

```
s3://file-tracker-backup/
  stor1/userA/config        ← restic repo
  stor1/userA/data/...
  stor1/userA/snapshots/...
  stor1/userB/config        ← restic repo
  stor1/userB/data/...
  stor2/deptC/config        ← restic repo
  ...
```

단일 버킷, prefix로 repo 분리. repo 수 제한 없음.

### 4.3 repo 자동 초기화

첫 이벤트 시 해당 repo가 없으면 자동 생성:

```python
initialized_repos = set()  # 메모리 캐시

def ensure_repo(repo_id):
    if repo_id in initialized_repos:
        return
    repo_url = f"s3:http://minio:9000/file-tracker-backup/{repo_id}"
    result = subprocess.run(["restic", "cat", "config", "-r", repo_url],
                            capture_output=True)
    if result.returncode != 0:
        subprocess.run(["restic", "init", "-r", repo_url])
    initialized_repos.add(repo_id)
```

### 4.4 컴포넌트

```
┌──────────────────────────────────────────────────────┐
│  backup-consumer                                      │
│                                                       │
│  ┌─────────────┐   ┌──────────────────────────────┐  │
│  │ Kafka       │──▶│ Event Processor               │  │
│  │ Consumer    │   │ - 이벤트 파싱                   │  │
│  └─────────────┘   │ - 경로 기준 Redis HSET         │  │
│                     │ - hostname 무시 (Lustre 공유)   │  │
│                     └──────────────────────────────┘  │
│                                                       │
│  ┌──────────────────────────────────────────────────┐ │
│  │ Backup Scheduler                                  │ │
│  │ 1. HGETALL pending → 전체 변경 파일 목록           │ │
│  │ 2. repo별 그룹핑 (get_repo_id)                    │ │
│  │ 3. repo별 태스크 → ThreadPoolExecutor              │ │
│  │ 4. 빈 worker가 다음 repo 태스크를 가져감           │ │
│  │ 5. 완료 시 HDEL + Kafka offset commit             │ │
│  └──────────────────────────────────────────────────┘ │
│                                                       │
│  ┌────────────────┐  ┌──────────┐                    │
│  │ Worker Pool    │  │  Redis   │                    │
│  │ (N개 스레드)   │  │ pending  │                    │
│  └───────┬────────┘  └──────────┘                    │
│          │                                            │
│          ▼                                            │
│  ┌──────────────────────────────────────────┐        │
│  │ MinIO (S3)                                │        │
│  │ s3://file-tracker-backup/{repo_id}/       │        │
│  │ - repo별 독립 restic 리포지토리             │        │
│  └──────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────┘
```

### 4.5 변경 DB (Redis)

```
HSET pending "/home/stor1/userA/docs/a.txt" '{"event":"mtime_change","ts":1710000000}'
HSET pending "/home/stor1/userA/data/b.txt" '{"event":"delete","ts":1710000100}'
HSET pending "/home/stor2/deptC/proj/c.txt" '{"event":"rename","old_path":"...","ts":1710000200}'
```

- `HSET`: 같은 경로면 덮어쓰기 (upsert, 중복 제거)
- `HLEN pending`: 대기 건수
- `HGETALL pending`: 전체 조회 → repo별 그룹핑
- `HDEL pending <path>`: 백업 완료 후 제거

### 4.6 이벤트 처리 규칙

| Kafka 이벤트 | Redis 동작 |
|-------------|-----------|
| `mtime_change` path=A | `HSET pending A '{"event":"mtime_change"}'` |
| `delete` path=A | `HSET pending A '{"event":"delete"}'` |
| `rename` old=A new=B | `HDEL pending A` + `HSET pending B '{"event":"rename","old_path":"A"}'` |

### 4.7 백업 실행 흐름

```python
def run_backup():
    # 1. Redis에서 전체 변경 파일 추출
    all_items = redis.hgetall("pending")

    # 2. repo별 그룹핑
    tasks = {}  # {repo_id: {path: info, ...}}
    for path, info in all_items.items():
        repo_id = get_repo_id(path)
        tasks.setdefault(repo_id, {})[path] = info

    # 3. Worker pool이 repo 태스크를 처리 (로드밸런싱)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(backup_repo, repo_id, files): repo_id
            for repo_id, files in tasks.items()
        }
        for future in as_completed(futures):
            future.result()  # 에러 처리

    # 4. Kafka offset commit
    consumer.commit()

def backup_repo(repo_id, files):
    ensure_repo(repo_id)
    repo_url = f"s3:http://minio:9000/file-tracker-backup/{repo_id}"

    # mtime_change + rename만 백업 (delete는 파일이 없으므로 제외)
    backup_list = [p for p, info in files.items()
                   if json.loads(info)["event"] != "delete"]

    if backup_list:
        # 파일 목록을 임시 파일에 작성
        list_file = f"/tmp/restic-{repo_id.replace('/', '-')}.txt"
        with open(list_file, "w") as f:
            f.write("\n".join(backup_list))

        subprocess.run([
            "restic", "backup",
            "--files-from", list_file,
            "-r", repo_url,
            "--tag", "incremental"
        ], check=True)
        os.unlink(list_file)

    # Redis에서 처리된 항목 제거
    for path in files:
        redis.hdel("pending", path)
```

**로드밸런싱 동작:**

```
tasks: {
    "stor1/userA": 10건,
    "stor1/userB": 5건,
    "stor2/bigDept": 3000건,
    "stor2/userC": 2건,
}

Worker 0 → stor1/userA (10건) → 완료 → stor2/userC (2건) → 완료 → 대기
Worker 1 → stor1/userB (5건) → 완료 → 대기
Worker 2 → stor2/bigDept (3000건) → ... 처리 중 ...
```

작은 repo를 먼저 끝낸 worker가 놀지 않고 다음 태스크를 가져감.

### 4.8 스냅샷 관리

```bash
# 특정 사용자의 스냅샷 목록
restic snapshots -r s3:http://minio:9000/file-tracker-backup/stor1/userA

# 시점 복원
restic restore <snapshot-id> \
  --target /restore \
  --include /home/stor1/userA/docs/a.txt \
  -r s3:http://minio:9000/file-tracker-backup/stor1/userA

# 오래된 스냅샷 정리 (90일 보존)
restic forget --keep-within 90d --prune \
  -r s3:http://minio:9000/file-tracker-backup/stor1/userA
```

경로에서 repo를 바로 특정:
```
"/home/stor1/userA/docs/a.txt" 복원
→ depth=2 → repo = "stor1/userA"
→ restic -r s3://.../stor1/userA
```

### 4.9 풀스캔 보정

이벤트 유실 대비 주기적 풀스캔:

```
매주 일요일 새벽:
  1. Lustre에서 find /home -type f -newer <last_fullscan_marker> 실행
  2. 결과를 Redis pending에 추가
  3. 정상 백업 흐름으로 처리
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

repo를 depth 기반으로 분리하여 동시 쓰기 문제 해결. `--files-from`으로 변경 파일만 깔끔하게 백업.

### 5.3 변경 DB: Redis

- 단일 Hash (`pending`) 하나로 관리
- `HSET`으로 upsert (중복 제거)
- `HGETALL`로 전체 조회 → repo별 그룹핑

## 6. 설정

```toml
# /etc/backup-consumer/config.toml

[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[backup]
schedule = "0 3 * * *"              # 매일 새벽 3시
restic_binary = "/usr/bin/restic"
workers = 10                        # 병렬 worker 수
base_path = "/home"                 # Lustre 마운트 포인트
repo_depth = 2                      # repo 분리 depth

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
| **B-2** | restic + MinIO 연동: 단일 repo 증분 백업 | MinIO, restic | 1일 |
| **B-3** | repo 분리 + 병렬: depth 기반 repo + worker pool | | 1일 |
| **B-4** | 스케줄러: cron 백업 + 스냅샷 정리 (forget/prune) | | 반나절 |
| **B-5** | 풀스캔 보정: 주기적 전체 비교 + 차분 백업 | | 반나절 |
| **B-6** | CLI: 스냅샷 조회, 파일 복원, 상태 확인 | | 1일 |
| **B-7** | 운영: systemd, 로깅, 모니터링, 문서 | | 반나절 |

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
