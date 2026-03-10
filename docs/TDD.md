# TDD: 파일 변경/삭제 실시간 추적 시스템 기술 설계

## 1. 아키텍처 개요

```
┌─────────────────────────────────────────────────┐
│                   Linux Kernel                   │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ kprobe:  │  │ kprobe:  │  │ kprobe:       │  │
│  │ unlink   │  │ vfs_write│  │ utimensat     │  │
│  └────┬─────┘  └────┬─────┘  └──────┬────────┘  │
│       │              │               │           │
│       └──────────┬───┘───────────────┘           │
│                  │                                │
│          ┌───────▼────────┐                      │
│          │  BPF Ring Buf  │                      │
│          └───────┬────────┘                      │
└──────────────────┼───────────────────────────────┘
                   │
┌──────────────────┼───────────────────────────────┐
│   Userspace      │                               │
│          ┌───────▼────────┐                      │
│          │  Event Reader  │                      │
│          └───────┬────────┘                      │
│                  │                                │
│          ┌───────▼────────┐                      │
│          │   Dedup Filter │  (time-window)       │
│          └───────┬────────┘                      │
│                  │                                │
│          ┌───────▼────────┐                      │
│          │  Kafka Producer│◄──┐                  │
│          └───────┬────────┘   │                  │
│                  │            │                  │
│            ┌─────▼─────┐  ┌──┴───┐              │
│            │   Kafka   │  │  WAL │              │
│            └───────────┘  └──────┘              │
└──────────────────────────────────────────────────┘
```

각 노드에서 독립 실행. 중앙 서버 불필요.

## 2. eBPF 프로그램 설계

### 2.1 후킹 대상

VFS 레벨 함수를 kprobe로 후킹하여 파일시스템 무관 동작 보장.

| 이벤트 | 후킹 포인트 | 비고 |
|--------|------------|------|
| 삭제 | `vfs_unlink` | unlink, unlinkat 포괄 |
| 쓰기 | `vfs_write` | write, pwrite64 포괄 |
| 절단 | `do_truncate` | truncate, ftruncate 포괄 |
| 시간변경 | `vfs_utimes` 또는 `do_utimes` | utimensat, utime 포괄 |

> VFS 함수 후킹으로 syscall 개별 후킹 대비 후크 수를 줄이고, 파일시스템 계층 무관 동작을 보장한다.

### 2.2 경로 필터링 (커널 레벨)

```c
// BPF 프로그램 내 경로 prefix 검사
// d_path()로 full path 획득 후 /home prefix 확인
// 불일치 시 즉시 return → ring buffer 미전송

SEC("kprobe/vfs_unlink")
int trace_unlink(struct pt_regs *ctx) {
    // 1. dentry에서 경로 추출
    // 2. /home prefix 검사 → 불일치 시 return 0
    // 3. ring buffer로 이벤트 전송
}
```

**주의**: 커널 5.14에서 `bpf_d_path()`는 제한적. `task_file` 기반 접근이 필요할 수 있음. 대안:
- kretprobe에서 반환값 확인 후 경로 해석
- dentry 구조체에서 직접 이름 추출 (부모 순회)

### 2.3 이벤트 구조체

```c
struct file_event {
    u64 ts_ns;          // ktime_get_ns()
    u32 event_type;     // 0=delete, 1=mtime_change
    char path[4096];    // full path
};
```

### 2.4 Ring Buffer

- BPF ring buffer 사용 (perf buffer 대비 오버헤드 낮음)
- 기본 크기: 16MB (튜닝 가능)
- 오버플로 시 드롭 카운터 증가 → 유저스페이스에서 모니터링

## 3. 유저스페이스 데몬 설계

### 3.1 기술 선택

| 항목 | 선택 | 사유 |
|------|------|------|
| 언어 | C 또는 Rust | 저수준 제어, 낮은 오버헤드 |
| BPF 라이브러리 | libbpf (CO-RE) | RHEL9 기본 지원, BTF 활용 |
| Kafka 클라이언트 | librdkafka | C/Rust 바인딩 성숙 |
| WAL | 커스텀 (append-only file) | 단순 구조, 외부 의존성 제거 |

### 3.2 이벤트 처리 파이프라인

```
Ring Buffer Poll
    │
    ▼
경로 유효성 검사 (path가 비어있거나 해석 실패 시 폐기)
    │
    ▼
중복 제거 필터
    │  - HashMap<path, last_event_ts>
    │  - mtime_change: 동일 경로 1초 윈도우 내 중복 제거
    │  - delete: 중복 제거 없이 즉시 통과
    ▼
직렬화 (JSON)
    │
    ▼
Kafka 전송 시도
    │
    ├─ 성공 → 완료
    └─ 실패 → WAL 기록
```

### 3.3 중복 제거 상세 (Debounce)

mtime 변경은 단일 파일에 대해 초당 수백 회 발생 가능 (대용량 write). 중복 제거 없이는 Kafka 폭주.

**Debounce 방식**: 이벤트가 계속 들어오는 동안은 전송하지 않고, 마지막 이벤트 후 quiet period(기본 1초) 동안 새 이벤트가 없으면 그때 1건만 전송.

**Max wait**: debounce 중 무한 write(로그 파일 등)로 타이머가 영원히 리셋되는 것을 방지. 첫 이벤트 후 max_wait(기본 1시간) 경과 시 write가 계속돼도 강제 전송.

```
HashMap<PathBuf, DebouncerEntry>

struct DebouncerEntry {
    timer: TimerHandle,
    first_seen: Instant,
}

on_event(path, event_type):
    if event_type == DELETE:
        // 삭제는 즉시 전달, 진행 중인 debounce 타이머 취소
        cancel_timer(path)
        map.remove(path)
        send(delete_event)
        return

    // mtime_change → debounce
    if let Some(entry) = map.get(path):
        if now - entry.first_seen > max_wait(1h):
            // max wait 초과 → 강제 전송 후 리셋
            cancel_timer(path)
            send(mtime_change_event)
            entry.first_seen = now
        cancel_timer(path)
        entry.timer = schedule_after(1s, || {
            send(mtime_change_event(path))
            map.remove(path)
        })
    else:
        map.insert(path, DebouncerEntry {
            timer: schedule_after(1s, || {
                send(mtime_change_event(path))
                map.remove(path)
            }),
            first_seen: now,
        })
```

엣지 케이스 처리:
- **무한 write**: max_wait(1h) 후 강제 전송, first_seen 리셋하여 다음 주기 시작
- **debounce 중 삭제**: 타이머 취소, delete만 전송 (mtime_change는 삭제된 파일이므로 무의미)
- **메모리**: active write 중인 파일 수만큼만 HashMap 유지, 타이머 만료 시 즉시 제거

debounce 윈도우와 max_wait 모두 설정 파일로 조정 가능.

### 3.4 WAL (Write-Ahead Log)

```
구조: append-only 바이너리 파일
┌──────────┬──────────┬──────────┐
│ header   │ record 1 │ record 2 │ ...
│ (magic,  │ (len,    │ (len,    │
│  version)│  data,   │  data,   │
│          │  crc32)  │  crc32)  │
└──────────┴──────────┴──────────┘
```

- 위치: `/var/lib/file-tracker/wal/`
- Kafka 전송 실패 시 WAL에 기록
- 백그라운드 스레드가 주기적으로 WAL → Kafka 재전송 시도
- 전송 완료된 레코드는 체크포인트로 표시
- WAL 파일이 임계치(예: 1GB) 초과 시 전송 완료분 truncate

### 3.5 Kafka 메시지 형식

```json
{
  "ts": 1710000000000,
  "event": "delete",
  "path": "/home/user1/data/file.txt",
  "hostname": "node-001"
}
```

- 토픽: `file-tracker-events` (설정 가능)
- 파티션 키: hostname (노드별 순서 보장)
- 압축: lz4

## 4. 설정

```toml
# /etc/file-tracker/config.toml

[watch]
paths = ["/home"]              # 감시 대상 경로 prefix
debounce_ms = 1000             # debounce quiet period (ms)
max_wait_sec = 3600            # 무한 write 강제 전송 주기 (초, 기본 1시간)

[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
compression = "lz4"
batch_size = 1000
linger_ms = 100

[wal]
dir = "/var/lib/file-tracker/wal"
max_size_mb = 1024

[ebpf]
ring_buffer_size_mb = 16

[logging]
level = "info"
file = "/var/log/file-tracker/agent.log"
```

## 5. 빌드 및 배포

### 5.1 빌드 요구사항

- RHEL9 빌드 환경
- clang/llvm (BPF 프로그램 컴파일)
- libbpf-devel
- librdkafka-devel
- kernel-devel + BTF 데이터 (`/sys/kernel/btf/vmlinux`)

### 5.2 산출물

```
/usr/bin/file-tracker           # 유저스페이스 데몬
/usr/lib/file-tracker/probe.o   # CO-RE BPF 오브젝트 (BTF 내장)
/etc/file-tracker/config.toml   # 설정 파일
/usr/lib/systemd/system/file-tracker.service  # systemd 유닛
```

### 5.3 systemd 유닛

```ini
[Unit]
Description=File Change Tracker Agent
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/file-tracker --config /etc/file-tracker/config.toml
Restart=always
RestartSec=5
LimitMEMLOCK=infinity
CapabilityBoundingSet=CAP_BPF CAP_PERFMON CAP_SYS_RESOURCE

[Install]
WantedBy=multi-user.target
```

## 6. 모니터링

에이전트는 다음 메트릭을 로그 또는 prometheus exporter로 노출:

| 메트릭 | 설명 |
|--------|------|
| `events_total{type}` | 감지된 이벤트 수 (delete/mtime_change) |
| `events_deduped_total` | 중복 제거된 이벤트 수 |
| `events_dropped_total` | ring buffer 오버플로로 드롭된 수 |
| `kafka_sent_total` | Kafka 전송 성공 수 |
| `kafka_failed_total` | Kafka 전송 실패 수 |
| `wal_size_bytes` | WAL 파일 현재 크기 |
| `wal_pending_records` | 미전송 WAL 레코드 수 |

## 7. 경로 해석 전략

커널에서 full path 획득은 eBPF의 주요 난제 중 하나.

### 전략 1: bpf_d_path() (선호)
- 커널 5.10+에서 지원
- 특정 BPF 프로그램 타입에서만 사용 가능 (fentry/fexit)
- RHEL9 커널 5.14에서 사용 가능 여부 검증 필요

### 전략 2: dentry 수동 순회
- dentry → d_parent 체인을 따라 루트까지 순회
- d_name에서 각 컴포넌트 추출 후 역순 조합
- BPF 루프 제한(bounded loop)으로 깊이 제한 필요 (예: 최대 32단계)

### 전략 3: fd → /proc/self/fd 유저스페이스 해석
- 커널에서는 fd만 전달
- 유저스페이스에서 readlink(`/proc/<pid>/fd/<fd>`)
- 프로세스 종료 시 fd 해석 불가 → 삭제 이벤트에서 문제

**결정**: 전략 2를 기본으로 사용. 전략 1은 RHEL9에서 검증 후 가능하면 전환.

## 8. 알려진 제약

| 제약 | 영향 | 대응 |
|------|------|------|
| 경로 깊이 32단계 제한 | 극단적 중첩 디렉토리 미지원 | 설정으로 조정 가능 |
| rename 미추적 | rename은 mtime 미변경 | PRD 범위 외 (필요 시 확장) |
| hardlink 삭제 | nlink > 1일 때 실제 삭제 아님 | 이벤트에 unlink로 기록, 소비자가 판단 |
| NFS 등 원격 FS | 서버 측 변경은 클라이언트 eBPF 미감지 | 각 노드 로컬 이벤트만 추적 (PRD 범위) |

## 9. 구현 단계

| 단계 | 내용 | 산출물 |
|------|------|--------|
| Phase 1 | eBPF probe + 유저스페이스 이벤트 출력 (stdout) | 동작 검증 |
| Phase 2 | 중복 제거 + Kafka 연동 | 기본 기능 완성 |
| Phase 3 | WAL + 재전송 | 안정성 확보 |
| Phase 4 | 설정 파일 + systemd + 패키징 | 배포 준비 |
| Phase 5 | 모니터링 메트릭 + 운영 문서 | 운영 준비 |
