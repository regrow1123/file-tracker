# PRD: 파일 변경 추적 및 증분 스냅샷 백업 시스템

## 1. 개요

대규모 리눅스 서버 환경(수백 대)에서 Lustre 공유 파일시스템의 `/home` 하위 파일 삭제, 변경, 이름변경을 실시간 감지하여 Kafka로 전달하고, 변경된 파일만 선별적으로 백업 스토리지에 증분 스냅샷하는 시스템.

**두 개의 컴포넌트로 구성:**

| 컴포넌트 | 위치 | 역할 |
|---------|------|------|
| **file-tracker** (에이전트) | 각 노드 | eBPF로 파일 이벤트 감지 → Kafka 전송 |
| **backup-consumer** (소비자) | 중앙 서버 (Lustre 마운트) | Kafka 이벤트 소비 → 변경 파일만 restic으로 백업 |

## 2. 배경

- 수PB 규모, 수백억 파일이 분산된 Lustre 공유 파일시스템
- 모든 노드가 동일한 `/home`을 공유 마운트
- 기존 백업: rsync 풀스캔 → 수시간~수일 소요 (수백억 파일 순회)
- 파일 변경/삭제 이력 추적 필요
- auditd는 고부하로 부적합 → eBPF 기반 접근
- 파일시스템 벤더에 무관하게 VFS 레벨에서 동작해야 함

## 3. 목표

### 3.1 에이전트 (file-tracker)

| 항목 | 내용 |
|------|------|
| 감지 대상 | 파일 삭제(unlink), mtime 변경(write/truncate/utimes), 이름변경(rename) |
| 감시 범위 | `/home` 이하 전체 (설정 가능) |
| 출력 데이터 | 변경/삭제/rename된 파일의 전체 경로 |
| 전달 방식 | Kafka (at-least-once) |
| 동작 범위 | 각 노드 로컬 (중앙 집중 불필요) |

### 3.2 소비자 (backup-consumer)

| 항목 | 내용 |
|------|------|
| 입력 | Kafka 이벤트 스트림 |
| Kafka 소비 | 수동 커밋 + 배치 pipeline (at-least-once) |
| 변경 DB | Redis Hash — 경로 기준 중복 제거 |
| 백업 도구 | restic (청크 dedup, `--files-from`, 스냅샷 관리) |
| 백업 스토리지 | MinIO (S3 호환 오브젝트 스토리지) |
| repo 구조 | 경로 depth 기반 분리 (depth=2: 스토리지/사용자별 repo) |
| 병렬 백업 | ThreadPoolExecutor + repo 단위 태스크 큐 |
| 동시 실행 방지 | Redis 분산 락 (멀티노드 대응) |
| 장애 복구 | Redis/Kafka 재연결, processing 복구, restic 재시도 |
| 모니터링 | Prometheus exporter (:9101/metrics) |
| 스케줄 | systemd timer (백업: 매일 03:00, prune: 매주 일 04:00) |
| 복원 | restic 스냅샷에서 시점 기반 파일 복원 |
| 스케일링 | Kafka 파티션 10개, consumer 수평 확장 가능 |

## 4. 비목표

- uid/pid 등 주체 식별
- 파일 내용의 실시간 스트리밍
- 중앙 집중형 에이전트 관리 서버
- 파일시스템 레벨 스냅샷 (btrfs/ZFS)

## 5. 대상 환경

| 항목 | 사양 |
|------|------|
| OS | RHEL 9, RHEL 10 |
| 커널 | 5.14+ (RHEL9), 6.12+ (RHEL10) — eBPF CO-RE, BTF 지원 |
| 서버 규모 | 수백 대 |
| 파일시스템 | Lustre (공유 마운트, VFS 레벨 후킹) |
| 디렉토리 구조 | `/home/{스토리지명}/{사용자 또는 부서}/...` |
| Kafka | 10+ 파티션, 파티션 키=hostname |
| 백업 스토리지 | MinIO (S3 호환) |
| 변경 DB | Redis (AOF 영속화) |

## 6. 기능 요구사항

### 6.1 에이전트: 이벤트 감지

- **삭제 감지**: `vfs_unlink` kprobe
- **mtime 변경 감지**: `vfs_write`, `do_truncate`, `vfs_utimes` kprobe
- **이름변경 감지**: `vfs_rename` kprobe
- **경로 필터**: `/home` prefix가 아닌 경로는 유저스페이스에서 폐기
- **커널 dedup**: inode 기반 LRU hash map으로 1초 내 중복 mtime 이벤트 억제

### 6.2 에이전트: 이벤트 출력

```json
{"ts":1710000000000,"event":"delete","path":"/home/stor1/userA/file.txt","hostname":"node-001"}
{"ts":1710000000000,"event":"mtime_change","path":"/home/stor1/userA/file.txt","hostname":"node-001"}
{"ts":1710000000000,"event":"rename","old_path":"/home/stor1/userA/old.txt","new_path":"/home/stor1/userA/new.txt","hostname":"node-001"}
```

### 6.3 에이전트: 전달

- Kafka 토픽으로 실시간 전송 (파티션 키 = hostname)
- 전송 실패 시 로컬 WAL에 버퍼링
- Kafka 복구 후 WAL에서 재전송 (at-least-once)

### 6.4 에이전트: Debounce

- mtime 변경: quiet period 10초, max wait 1시간
- 삭제/rename: debounce 없이 즉시 전달

### 6.5 소비자: 변경 파일 수집

- Kafka 수동 커밋 + 배치 pipeline (100건/5초)
- Redis pipeline flush 성공 후에만 Kafka commit → at-least-once
- HSET idempotent → 중복 처리 무해
- 이벤트 처리 규칙:
  - `mtime_change` → `HSET pending path "mtime_change"`
  - `delete` → 무시 (이전 스냅샷에 보존)
  - `rename` → pipeline: `HDEL old + HSET new`

### 6.6 소비자: 증분 백업 실행

- systemd timer (매일 03:00) 또는 수동 트리거
- **분산 락**: `SET backup:lock NX EX 7200` (멀티노드 동시 실행 방지)
- **RENAME**: pending → processing 원자적 swap (consumer와 간섭 차단)
- **processing 복구**: 이전 중단 시 processing 잔존 → 이어서 처리
- **HSCAN**: 1만 건 배치 (Redis 블로킹 방지)
- **repo별 그룹핑** → ThreadPoolExecutor로 병렬 백업
- **restic --files-from**: 변경 파일만 선별 백업
- **재시도**: 3회 (10s, 20s backoff), 최종 실패 시 pending에 복원
- **결과 기록**: Redis `backup:last_run` Hash (Prometheus 노출)

### 6.7 소비자: 스냅샷 복원

- restic 스냅샷에서 시점 선택 → 파일/디렉토리 복원
- 경로에서 repo를 바로 특정 가능 (depth 규칙)
- CLI: `python3 cli.py restore <path> [-s snapshot_id] [-t target]`

### 6.8 소비자: 모니터링

Prometheus exporter (`:9101/metrics`):

| 메트릭 | 타입 | 설명 |
|--------|------|------|
| `backup_events_processed_total` | counter | 처리된 이벤트 |
| `backup_events_skipped_delete_total` | counter | 무시된 delete |
| `backup_events_errors_total` | counter | 처리 에러 |
| `backup_events_redis_errors_total` | counter | Redis pipeline 실패 |
| `backup_pending_total` | gauge | Redis pending 건수 |
| `backup_last_run_*` | gauge | 마지막 백업 결과 |
| `backup_kafka_errors_total{type}` | counter | Kafka 에러 (유형별) |
| `backup_kafka_commit_errors_total` | counter | Kafka commit 실패 |

### 6.9 소비자: 장애 복구

| 장애 시나리오 | 처리 |
|-------------|------|
| 시작 시 Kafka 다운 | 브로커 연결 확인 → 5초 간격 무한 재시도 |
| 운영 중 Kafka 다운 | librdkafka 자동 재연결 + 에러 콜백 |
| commit 실패 | catch → 다음 배치에서 재커밋 |
| 시작 시 Redis 다운 | 5초 간격 무한 재시도 (consumer) / 3회 재시도 (backup) |
| 운영 중 Redis 다운 | flush 실패 → commit 안 함 → 재연결 → Kafka 재수신 |
| backup 프로세스 kill | processing 키 잔존 → 다음 실행에서 이어서 |
| backup 동시 실행 | Redis 분산 락으로 방지 |
| restic 실패 | 3회 재시도 → 실패 paths를 pending에 복원 |

## 7. 비기능 요구사항

### 7.1 에이전트

| 항목 | 요구 |
|------|------|
| CPU 오버헤드 | 노드당 < 1% |
| 메모리 | < 50MB RSS |
| 이벤트 지연 | 감지 → Kafka < 3초 (mtime은 debounce 후) |
| WAL 보존 | Kafka 전달 완료까지, 최대 1GB |
| 커널 안전성 | eBPF verifier 통과, 커널 패닉 유발 불가 |

### 7.2 소비자

| 항목 | 요구 |
|------|------|
| 처리량 | 수백 노드의 이벤트를 처리 (수평 확장 가능) |
| 이벤트 보장 | at-least-once (수동 커밋) |
| 백업 신뢰성 | 실패 재시도 + pending 복원 |
| 스토리지 효율 | restic 청크 dedup으로 중복 데이터 최소화 |
| 복원 시간 | 단일 파일 복원 < 1분 |
| 모니터링 | Prometheus 메트릭 실시간 노출 |

## 8. 제약 및 리스크

| 리스크 | 대응 |
|--------|------|
| 이벤트 수신~백업 사이 파일 재변경 | 다음 이벤트에서 재백업, 최종 일관성 |
| 소비자 장기 다운 시 Kafka retention 초과 | 풀스캔 백업으로 보정 (주 1회 등) |
| 대용량 파일 백업 시 네트워크 부하 | restic 청크 dedup으로 변경분만 전송 |
| MinIO 장애 | restic 재시도, 소비자 일시 정지 |
| 같은 파일의 다중 노드 이벤트 | Redis 경로 기준 upsert로 중복 제거 |
| restic repo 분리 후 병합 불가 | 초기 depth 설계 중요 |

## 9. 성공 지표

- 삭제/변경/rename 이벤트 감지율 > 99.9%
- 에이전트 노드당 CPU < 1%
- 증분 백업 시간: 풀스캔 대비 90% 이상 단축
- 시점 복원: 임의 시점의 파일 복원 가능
- 스토리지 효율: restic dedup으로 원본 대비 50% 이상 절감
- E2E 테스트: 43/43 통과 (happy path + edge case)
