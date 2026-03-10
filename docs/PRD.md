# PRD: 파일 변경/삭제 실시간 추적 시스템

## 1. 개요

대규모 리눅스 서버 환경(수백 대)에서 홈 디렉토리 하위 파일의 삭제 및 mtime 변경을 실시간으로 감지하여 Kafka로 전달하는 경량 에이전트.

## 2. 배경

- 수PB 규모, 수백억 파일이 분산된 환경
- 파일 변경/삭제 이력 추적 필요
- auditd는 고부하로 부적합 → eBPF 기반 접근
- 파일시스템 벤더에 무관하게 VFS 레벨에서 동작해야 함

## 3. 목표

| 항목 | 내용 |
|------|------|
| 감지 대상 | 파일 삭제(unlink), mtime 변경(write/truncate/utimes 등) |
| 감시 범위 | `/home` 이하 전체 |
| 출력 데이터 | 변경/삭제된 파일의 전체 경로 |
| 전달 방식 | Kafka |
| 지연 시간 | 실시간 (초 단위) |
| 동작 범위 | 각 노드 로컬 (중앙 집중 불필요) |

## 4. 비목표

- uid/pid 등 주체 식별
- 파일 내용 수집
- 중앙 집중형 관리 서버
- 파일 복구 기능

## 5. 대상 환경

| 항목 | 사양 |
|------|------|
| OS | RHEL 9 |
| 커널 | 5.14+ (eBPF CO-RE, BTF 지원) |
| 서버 규모 | 수백 대 |
| 파일시스템 | 무관 (VFS 레벨 후킹) |

## 6. 기능 요구사항

### 6.1 이벤트 감지

- **삭제 감지**: `unlink`, `unlinkat` syscall 후킹
- **mtime 변경 감지**:
  - 실제 쓰기: `write`, `pwrite64`, `writev`, `pwritev`
  - 파일 절단: `truncate`, `ftruncate`
  - 명시적 시간 변경: `utimensat`, `futimesat`, `utime`
- **경로 필터**: `/home` prefix가 아닌 경로는 커널 레벨에서 조기 폐기

### 6.2 이벤트 출력

각 이벤트는 최소한 다음을 포함:

```json
{
  "ts": 1710000000000,
  "event": "delete" | "mtime_change",
  "path": "/home/user1/data/file.txt",
  "hostname": "node-001"
}
```

### 6.3 전달

- Kafka 토픽으로 실시간 전송
- 전송 실패 시 로컬 WAL(Write-Ahead Log)에 버퍼링
- Kafka 복구 후 WAL에서 재전송 (at-least-once)

### 6.4 중복 제거 (Debounce)

- 동일 파일에 대한 mtime 변경 이벤트는 debounce 처리: 마지막 이벤트 후 quiet period(기본 1초) 경과 시 1건만 전달
- 무한 write 방지: 첫 이벤트 후 max wait(기본 1시간) 경과 시 강제 전송
- 삭제 이벤트는 debounce 없이 즉시 전달

## 7. 비기능 요구사항

| 항목 | 요구 |
|------|------|
| CPU 오버헤드 | 노드당 < 1% (idle 기준) |
| 메모리 | < 50MB RSS |
| 이벤트 지연 | 감지 → Kafka 전달 < 3초 |
| WAL 보존 | Kafka 전달 완료까지 무제한 |
| 가용성 | 에이전트 재시작 시 WAL에서 미전달 이벤트 재전송 |
| 커널 안전성 | eBPF verifier 통과, 커널 패닉 유발 불가 |

## 8. 제약 및 리스크

| 리스크 | 대응 |
|--------|------|
| RHEL9 커널 5.14의 eBPF 제약 | CO-RE + BTF로 호환성 확보, kprobe 폴백 |
| 대량 write 시 이벤트 폭주 | 커널 레벨 경로 필터 + 유저스페이스 중복 제거 |
| 경로 해석 비용 (dentry → full path) | d_path helper 사용, 해석 실패 시 fd 기반 폴백 |
| ring buffer 오버플로 | 버퍼 크기 튜닝 + 드롭 카운터 모니터링 |

## 9. 성공 지표

- 삭제/mtime 변경 이벤트 감지율 > 99.9%
- 이벤트 드롭율 < 0.1%
- 노드당 CPU 오버헤드 < 1%
- Kafka 전달 지연 p99 < 3초
