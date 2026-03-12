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

# 에이전트 빌드
make build

# RPM 패키징
make rpm               # 에이전트 RPM
make rpm-consumer      # consumer RPM
```

## 설치

### 에이전트 (각 노드)

```bash
# RPM
sudo rpm -ivh file-tracker-1.0.0-1.el9.x86_64.rpm
sudo vi /etc/file-tracker/config.toml
sudo systemctl enable --now file-tracker

# 또는 수동
make install
```

### Consumer (중앙 서버)

```bash
# RPM
sudo rpm -ivh backup-consumer-1.0.0-1.noarch.rpm
sudo vi /etc/backup-consumer/config.toml
sudo systemctl enable --now backup-consumer

# 또는 수동
cd consumer && sudo bash deploy/install.sh
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
├── consumer/                  # 증분 백업 시스템 (Python)
│   ├── main.py                # consumer 데몬 (Kafka → Redis)
│   ├── consumer.py            # EventProcessor 라이브러리
│   ├── backup.py              # restic 증분 백업 (oneshot)
│   ├── prune.py               # 스냅샷 정리 (oneshot)
│   ├── cli.py                 # CLI (status/snapshots/restore)
│   ├── config.toml            # 설정
│   ├── requirements.txt       # Python 의존성
│   ├── README.md              # consumer 문서
│   └── deploy/                # systemd units + install.sh
├── deploy/
│   ├── file-tracker.service   # systemd unit
│   ├── config.toml            # 배포용 기본 설정
│   └── install.sh             # 수동 설치 스크립트
├── rpm/
│   └── file-tracker.spec      # RPM spec
├── docs/
│   ├── PRD.md                 # 제품 요구사항
│   └── TDD.md                 # 기술 설계
└── test/
    ├── e2e.sh                 # E2E 테스트 (happy path)
    ├── e2e-edge.sh            # E2E 엣지케이스 테스트
    ├── vm-centos9/            # RHEL 9 테스트 VM
    └── vm-centos10/           # RHEL 10 테스트 VM
```

## 운영 가이드

### Kafka 파티션

프로덕션에서는 10+ 파티션 권장 (파티션 키=hostname, 노드별 분산):

```bash
kafka-topics.sh --create --topic file-tracker-events \
  --partitions 10 --replication-factor 3 \
  --bootstrap-server kafka01:9092
```

### 다중 노드 배포

수백 대 노드에 동일 바이너리 + 동일 config 배포:

```bash
# RPM 배포
sudo rpm -ivh file-tracker-1.0.0-1.el9.x86_64.rpm
sudo vi /etc/file-tracker/config.toml   # brokers 주소만 변경
sudo systemctl enable --now file-tracker
```

### Consumer 배포

consumer는 `noarch` RPM이므로 아키텍처 무관:

```bash
# RPM 배포
sudo rpm -ivh backup-consumer-1.0.0-1.noarch.rpm

# config: Kafka brokers, Redis URL 설정
sudo vi /etc/backup-consumer/config.toml

# Prometheus 메트릭 확인
curl http://localhost:9101/metrics
```

자세한 내용은 [`consumer/README.md`](consumer/README.md) 참조.

### 백업 시스템 연동

consumer가 생성하는 Redis `pending` Hash를 소비하는 백업 시스템은 별도 구성.
참조 구현: [`backup/`](backup/) 디렉토리.

## 테스트

```bash
# E2E 테스트 (happy path) — agent→kafka→consumer→backup→restore→prune
sudo bash test/e2e.sh      # 24개 체크포인트

# E2E 엣지케이스 — Redis 장애, 동시실행, processing 복구, 대량 파일 등
sudo bash test/e2e-edge.sh  # 19개 체크포인트
```

## 라이선스

GPL-2.0 (eBPF 프로그램은 GPL 필수)
