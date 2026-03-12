# backup-consumer

Kafka 이벤트 소비 → Redis 변경 DB (pending Hash)

## 역할

file-tracker 에이전트가 전송한 Kafka 이벤트를 소비하여 Redis `pending` Hash에 변경 파일 경로를 기록한다. 이후 백업 처리는 별도 시스템이 Redis `pending`을 읽어 수행한다.

## 특징

- **at-least-once 보장**: 수동 Kafka 커밋 — Redis 쓰기 성공 후에만 offset 커밋
- **배치 파이프라인**: 100건 또는 5초 간격으로 Redis pipeline 일괄 실행
- **자동 장애 복구**: Kafka/Redis 재연결, 에러 콜백, commit 재시도
- **Prometheus 모니터링**: 이벤트 처리량, pending 건수, Kafka 에러
- **수평 확장**: Kafka consumer group 기반 — 프로세스 추가만으로 스케일 아웃

## 아키텍처

```
file-tracker (각 노드)
    │
    ▼
  Kafka (10 파티션, 파티션 키=hostname)
    │
    ▼
backup-consumer.service (상시 실행)
  │  Kafka poll → parse_event (배치 큐)
  │  → 100건/5초 → Redis pipeline flush
  │  → 성공 → Kafka commit
  │  → 실패 → commit 안 함 → Kafka 재수신
  │
  ▼
Redis (pending Hash)
  │
  ▼
(백업 시스템이 pending 소비)
```

## Redis 데이터 구조

```
Hash: pending
  "/home/stor1/userA/docs/a.txt" → "mtime_change"
  "/home/stor1/userB/data/b.txt" → "rename"
  "/home/stor2/deptC/proj/c.txt" → "mtime_change"
```

### 이벤트 처리 규칙

| Kafka 이벤트 | Redis 동작 |
|-------------|-----------|
| `mtime_change` path=A | `HSET pending A "mtime_change"` |
| `delete` path=A | 무시 (백업 대상 아님) |
| `rename` old=A new=B | pipeline: `HDEL pending A` + `HSET pending B "rename"` |

- **HSET idempotent**: 같은 경로 중복 이벤트는 자연 덮어쓰기
- **ts 비교 없음**: pending은 "이 파일을 확인하라"는 마커

## 파일 구성

| 파일 | 역할 |
|------|------|
| `main.py` | consumer 데몬: Kafka 수동 커밋, 배치 pipeline, Prometheus, 장애 복구 |
| `consumer.py` | EventProcessor: parse_event (파싱), flush_batch (Redis pipeline) |
| `config.toml` | 설정 |

## 설정

`/etc/backup-consumer/config.toml`:

```toml
[kafka]
brokers = "kafka01:9092,kafka02:9092"
topic = "file-tracker-events"
group_id = "backup-consumer"

[db]
redis_url = "redis://localhost:6379/0"

[logging]
level = "INFO"                  # DEBUG, INFO, WARNING, ERROR

[metrics]
port = 9101                     # Prometheus HTTP 포트
```

## 설치

```bash
cd consumer
sudo bash deploy/install.sh
sudo vi /etc/backup-consumer/config.toml

sudo systemctl enable --now backup-consumer
```

## 스케일링

Kafka 파티션 10개, consumer group 기반 수평 확장:

```
consumer 1대 → 파티션 10개 전부
consumer 2대 → 각 5개씩 (자동 리밸런싱)
consumer 10대 → 각 1개씩 (최대)
```

같은 `group_id`로 프로세스 추가 시 Kafka가 자동 리밸런싱.

## Prometheus 메트릭 (`:9101/metrics`)

| 메트릭 | 타입 | 설명 |
|--------|------|------|
| `backup_events_processed_total` | counter | 처리된 이벤트 |
| `backup_events_skipped_delete_total` | counter | 무시된 delete |
| `backup_events_errors_total` | counter | 처리 에러 |
| `backup_events_redis_errors_total` | counter | Redis pipeline 실패 |
| `backup_pending_total` | gauge | Redis pending 건수 |
| `backup_kafka_errors_total{type}` | counter | Kafka 에러 (유형별) |
| `backup_kafka_commit_errors_total` | counter | Kafka commit 실패 |

## 장애 복구

| 시나리오 | 처리 |
|---------|------|
| Kafka 시작 시 다운 | 5초 간격 무한 재시도 (list_topics 확인) |
| Kafka 운영 중 다운 | librdkafka 자동 재연결 + error_cb 메트릭 |
| Kafka commit 실패 | 다음 배치에서 자연 재커밋 |
| Redis 시작 시 다운 | 5초 간격 무한 재시도 |
| Redis 운영 중 다운 | flush 실패 → commit 안 함 → 복구 후 Kafka 재수신 |

## 백업 시스템 연동

이 consumer가 생성하는 Redis `pending` Hash를 소비하는 백업 시스템은 [`backup/`](../backup/) 디렉토리에 참조 구현이 있습니다.

백업 시스템이 지켜야 할 규칙:
1. `RENAME pending processing` 으로 원자적 swap 후 작업 (consumer와 간섭 방지)
2. `HSCAN`으로 배치 조회 (Redis 블로킹 방지)
3. 작업 완료 후 `DELETE processing`
4. 동시 실행 방지 필요 시 Redis 분산 락 사용 권장
