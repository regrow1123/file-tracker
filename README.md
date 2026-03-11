# file-tracker

eBPF 기반 실시간 파일 변경/삭제/이름변경 추적 에이전트.

`/home` 하위 파일의 변경 이벤트를 감지하여 Apache Kafka로 전송한다. 파일시스템 종류에 무관하게 VFS 레벨에서 동작하며, CO-RE(Compile Once, Run Everywhere)로 RHEL 9/10 단일 바이너리를 지원한다.

## 감지 이벤트

| 이벤트 | kprobe | 설명 | debounce |
|--------|--------|------|----------|
| `delete` | `vfs_unlink` | 파일 삭제 | 즉시 전송 |
| `mtime_change` | `vfs_write`, `do_truncate`, `vfs_utimes` | 파일 내용/시간 변경 | 10초 quiet, 1시간 max |
| `rename` | `vfs_rename` | 파일 이름 변경 | 즉시 전송 |

## Kafka 메시지 포맷

```json
{"ts":1773219882338,"event":"delete","path":"/home/user/file.txt","hostname":"node-001"}
{"ts":1773219891387,"event":"mtime_change","path":"/home/user/file.txt","hostname":"node-001"}
{"ts":1773219882338,"event":"rename","old_path":"/home/user/old.txt","new_path":"/home/user/new.txt","hostname":"node-001"}
```

- `ts`: 밀리초 단위 Unix timestamp
- 파티션 키: hostname (같은 노드의 이벤트는 같은 파티션으로 보장)

## 아키텍처

```
┌─────────────────────────────────────────────┐
│  Kernel                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ kprobe   │  │ inode    │  │ ring      │  │
│  │ 5개 hook │─▶│ dedup    │─▶│ buffer    │  │
│  └──────────┘  │ (LRU 1s) │  │ (16MB)    │  │
│                └──────────┘  └─────┬─────┘  │
├────────────────────────────────────┼────────┤
│  Userspace                         │        │
│                              ┌─────▼─────┐  │
│                              │ path 조립  │  │
│                              │ /home 필터 │  │
│                              └─────┬─────┘  │
│                    ┌───────────────┼────┐   │
│                    ▼               ▼    │   │
│              ┌──────────┐   ┌────────┐  │   │
│              │ debouncer│   │ rename │  │   │
│              │ 10s/1h   │   │ pairing│  │   │
│              └────┬─────┘   └───┬────┘  │   │
│                   └──────┬──────┘       │   │
│                          ▼              │   │
│                    ┌──────────┐         │   │
│                    │  Kafka   │◀── WAL ─┘   │
│                    │ producer │  (retry)    │
│                    └──────────┘              │
└─────────────────────────────────────────────┘
```

## 요구 사항

- RHEL 9 (kernel 5.14+) 또는 RHEL 10 (kernel 6.12+)
- BTF 활성화 (`/sys/kernel/btf/vmlinux` 존재)
- root 권한 (eBPF 필요)
- Apache Kafka 클러스터

## 빌드

```bash
# 빌드 의존성 설치
make deps              # RHEL 패키지 (clang, libbpf-devel, bpftool 등)
make deps-rdkafka      # librdkafka 2.3.0 소스 빌드

# 빌드
make build

# RPM 패키징
make rpm
# → ~/rpmbuild/RPMS/x86_64/file-tracker-1.0.0-1.el9.x86_64.rpm
```

## 설치

### RPM

```bash
sudo rpm -ivh file-tracker-1.0.0-1.el9.x86_64.rpm
```

### 수동

```bash
make install
```

## 설정

`/etc/file-tracker/config.toml`:

```toml
[kafka]
brokers = "kafka-broker-1:9092,kafka-broker-2:9092"
topic = "file-tracker-events"

[debounce]
quiet_ms = 10000       # 마지막 이벤트 후 대기 시간
max_wait_ms = 3600000  # 최대 대기 시간 (1시간)

[wal]
path = "/var/lib/file-tracker/wal.log"
max_size_mb = 1024     # WAL 최대 크기

[watch]
prefix = "/home"       # 감시 대상 경로

[logging]
level = "info"         # error, warn, info, debug
```

## 실행

```bash
# 서비스 시작
sudo systemctl enable --now file-tracker

# 상태 확인
sudo systemctl status file-tracker

# 로그
journalctl -u file-tracker -f
```

## 성능

130만 IOPS 부하 테스트 결과 (fio, 100개 파일, 60초):

| 항목 | 결과 |
|------|------|
| CPU | 0.24% |
| RSS | 43 MB |
| ring buffer drops | 0 |
| kafka 전송 실패 | 0 |

커널 내 inode 기반 LRU dedup (1초 간격)으로 중복 이벤트 99.99% 제거.

## 신뢰성

- **WAL (Write-Ahead Log)**: Kafka 전송 실패 시 로컬 WAL에 기록, 30초마다 재전송
- **CRC32 체크섬**: WAL 레코드 무결성 검증
- **at-least-once**: WAL 확인 전까지 이벤트 보존
- **graceful shutdown**: SIGTERM 시 debounce 중인 이벤트 전부 flush

## 트러블슈팅

### BPF 프로그램 로드 실패

```
Failed to open and load BPF skeleton
```

- BTF 확인: `ls /sys/kernel/btf/vmlinux`
- root 권한 확인
- `LimitMEMLOCK=infinity` 확인 (systemd)

### Kafka 연결 실패

```
kafka delivery failed: Local: Message timed out
```

- 브로커 주소 확인: `config.toml`의 `[kafka] brokers`
- 네트워크 확인: `nc -zv kafka-broker 9092`
- WAL에 자동 저장되며 30초마다 재전송 시도

### ring buffer drop 발생

```
Ring buffer dropped N events in last interval!
```

- 정상적인 /home 사용에서는 발생하지 않음
- 극단적 I/O (100만+ IOPS)에서만 발생 가능
- dedup이 동작하면 실질적으로 이벤트 손실 없음

## 디렉토리 구조

```
├── CMakeLists.txt
├── Makefile                  # deps, build, rpm, install
├── config.toml               # 개발용 설정
├── src/
│   ├── bpf/probe.bpf.c       # eBPF 프로그램 (5개 kprobe)
│   ├── main.cpp               # 메인 루프, 이벤트 처리
│   ├── config.cpp             # TOML 파싱
│   ├── debouncer.cpp          # debounce 로직
│   ├── kafka_producer.cpp     # librdkafka 래퍼
│   └── wal.cpp                # Write-Ahead Log
├── include/
│   ├── common.h               # 공용 상수, path 조립
│   ├── vmlinux.h              # 커널 구조체 (CO-RE)
│   └── *.h
├── deploy/
│   ├── file-tracker.service   # systemd unit
│   ├── config.toml            # 배포용 기본 설정
│   └── install.sh             # 수동 설치 스크립트
├── rpm/
│   └── file-tracker.spec      # RPM spec
└── test/
    ├── vm-centos9/            # RHEL 9 테스트 VM
    └── vm-centos10/           # RHEL 10 테스트 VM
```

## 라이선스

GPL-2.0 (eBPF 프로그램은 GPL 필수)
