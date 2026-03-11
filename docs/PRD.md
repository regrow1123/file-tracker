# PRD: 파일 변경 추적 및 증분 스냅샷 백업 시스템

## 1. 개요

대규모 리눅스 서버 환경(수백 대, Lustre)에서 `/home` 하위 파일의 삭제, 변경, 이름변경을 실시간 감지하여 Kafka로 전달하고, 변경된 파일만 선별적으로 백업 스토리지에 증분 스냅샷하는 시스템.

**두 개의 컴포넌트로 구성:**

| 컴포넌트 | 위치 | 역할 |
|---------|------|------|
| **file-tracker** (에이전트) | 각 노드 | eBPF로 파일 이벤트 감지 → Kafka 전송 |
| **backup-consumer** (소비자) | 중앙 서버 | Kafka 이벤트 소비 → 변경 파일만 restic으로 백업 |

## 2. 배경

- 수PB 규모, 수백억 파일이 분산된 Lustre 환경
- 기존 백업: rsync 풀스캔 → 수시간~수일 소요
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
| 기능 | 변경 파일 중복 제거 → 증분 백업 실행 |
| 백업 도구 | restic (청크 기반 dedup, 스냅샷 관리) |
| 백업 스토리지 | MinIO (S3 호환 오브젝트 스토리지) |
| 복원 | restic 스냅샷에서 시점 기반 파일 복원 |

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
| 파일시스템 | Lustre (VFS 레벨 후킹으로 무관) |
| 백업 스토리지 | MinIO (S3 호환) |

## 6. 기능 요구사항

### 6.1 에이전트: 이벤트 감지

- **삭제 감지**: `vfs_unlink` kprobe
- **mtime 변경 감지**: `vfs_write`, `do_truncate`, `vfs_utimes` kprobe
- **이름변경 감지**: `vfs_rename` kprobe
- **경로 필터**: `/home` prefix가 아닌 경로는 유저스페이스에서 폐기
- **커널 dedup**: inode 기반 LRU hash map으로 1초 내 중복 mtime 이벤트 억제

### 6.2 에이전트: 이벤트 출력

```json
{"ts":1710000000000,"event":"delete","path":"/home/user/file.txt","hostname":"node-001"}
{"ts":1710000000000,"event":"mtime_change","path":"/home/user/file.txt","hostname":"node-001"}
{"ts":1710000000000,"event":"rename","old_path":"/home/user/old.txt","new_path":"/home/user/new.txt","hostname":"node-001"}
```

### 6.3 에이전트: 전달

- Kafka 토픽으로 실시간 전송 (파티션 키 = hostname)
- 전송 실패 시 로컬 WAL에 버퍼링
- Kafka 복구 후 WAL에서 재전송 (at-least-once)

### 6.4 에이전트: Debounce

- mtime 변경: quiet period 10초, max wait 1시간
- 삭제/rename: debounce 없이 즉시 전달

### 6.5 소비자: 변경 파일 수집

- Kafka consumer group으로 이벤트 소비
- 변경 DB에 `(hostname, path)` 키로 upsert → 중복 제거
- 이벤트 타입별 상태 관리:
  - `mtime_change` → 백업 대상
  - `delete` → 백업에서 제거 (또는 마킹)
  - `rename` → old_path 제거 + new_path 백업

### 6.6 소비자: 증분 백업 실행

- 주기적 (매일 새벽 등) 또는 수동 트리거
- 변경 DB에서 노드별 변경 파일 목록 추출
- 각 노드에서 변경된 파일만 restic으로 백업:
  ```
  restic backup --files-from=changed_files.txt -r s3:http://minio:9000/backup
  ```
- 백업 완료 시 변경 DB 해당 항목 제거 + Kafka offset commit

### 6.7 소비자: 스냅샷 복원

- restic 스냅샷 목록 조회 → 시점 선택 → 파일/디렉토리 복원
  ```
  restic restore <snapshot-id> --target /restore --include /home/user/file.txt
  ```

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
| 처리량 | 수백 노드의 이벤트를 단일 소비자로 처리 가능 |
| 백업 신뢰성 | at-least-once (이벤트 유실 시 주기적 풀스캔 보정) |
| 스토리지 효율 | restic 청크 dedup으로 중복 데이터 최소화 |
| 복원 시간 | 단일 파일 복원 < 1분 |

## 8. 제약 및 리스크

| 리스크 | 대응 |
|--------|------|
| 이벤트 수신~파일 pull 사이 파일 재변경 | 다음 이벤트에서 재백업, 최종 일관성 |
| 소비자 장기 다운 시 Kafka retention 초과 | 풀스캔 백업으로 보정 (주 1회 등) |
| 대용량 파일 백업 시 네트워크 부하 | restic 청크 dedup으로 변경분만 전송 |
| MinIO 장애 | restic 재시도, 소비자 일시 정지 |

## 9. 성공 지표

- 삭제/변경/rename 이벤트 감지율 > 99.9%
- 에이전트 노드당 CPU < 1%
- 증분 백업 시간: 풀스캔 대비 90% 이상 단축
- 시점 복원: 임의 시점의 파일 복원 가능
- 스토리지 효율: restic dedup으로 원본 대비 50% 이상 절감
