#!/usr/bin/env python3
"""backup-consumer: Kafka 이벤트 → Redis 수집 데몬

수동 커밋 + 배치 pipeline:
  - N건(100) 또는 T초(5) 도달 시 Redis pipeline flush
  - flush 성공 후에만 Kafka commit → at-least-once 보장
  - Redis 장애 시 commit 안 함 → Kafka에서 재수신
  - Kafka 장애: librdkafka 자동 재연결, 에러 콜백으로 모니터링
  - HSET은 idempotent → 중복 처리 무해
"""

import signal
import sys
import time
import logging
from confluent_kafka import Consumer, KafkaError, KafkaException
from prometheus_client import start_http_server, Counter as PromCounter
import redis
import toml

from consumer import EventProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")

BATCH_SIZE = 100       # 배치 크기
BATCH_TIMEOUT = 5.0    # 배치 타임아웃 (초)
REDIS_RETRY_INTERVAL = 5  # Redis 재연결 간격 (초)
KAFKA_CONNECT_TIMEOUT = 30  # Kafka 시작 시 연결 확인 타임아웃 (초)
KAFKA_CONNECT_RETRY = 5     # Kafka 연결 실패 시 재시도 간격 (초)

# Kafka 에러 메트릭
KAFKA_ERRORS = PromCounter(
    "backup_kafka_errors_total",
    "Kafka broker/consumer errors",
    ["type"])
KAFKA_COMMIT_ERRORS = PromCounter(
    "backup_kafka_commit_errors_total",
    "Kafka commit failures")


def connect_redis(cfg: dict) -> redis.Redis:
    """Redis 연결. 실패 시 무한 재시도."""
    url = cfg["db"]["redis_url"]
    while True:
        try:
            r = redis.Redis.from_url(url, decode_responses=True,
                                     socket_connect_timeout=5,
                                     socket_timeout=5,
                                     retry_on_timeout=True)
            r.ping()
            return r
        except (redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError) as e:
            log.warning("Redis 연결 실패, %ds 후 재시도: %s",
                        REDIS_RETRY_INTERVAL, e)
            time.sleep(REDIS_RETRY_INTERVAL)


def create_kafka_consumer(cfg: dict) -> Consumer:
    """Kafka consumer 생성 + 브로커 연결 확인. 실패 시 무한 재시도."""

    kafka_connected = {"value": False}

    def error_cb(err):
        """librdkafka 에러 콜백. 브로커 장애/복구 시 호출."""
        code = err.code()
        if code == KafkaError._ALL_BROKERS_DOWN:
            log.error("Kafka: 모든 브로커 다운")
            KAFKA_ERRORS.labels(type="all_brokers_down").inc()
        elif code == KafkaError._TRANSPORT:
            log.warning("Kafka: 브로커 연결 끊김 (자동 재연결)")
            KAFKA_ERRORS.labels(type="transport").inc()
        else:
            log.warning("Kafka 에러: %s", err)
            KAFKA_ERRORS.labels(type="other").inc()

    def stats_cb(stats_json):
        """librdkafka 통계 콜백 (60초 간격)."""
        import json
        stats = json.loads(stats_json)
        log.debug("Kafka 통계: replyq=%s, msg_cnt=%s",
                  stats.get("replyq", "?"), stats.get("msg_cnt", "?"))

    while True:
        consumer = Consumer({
            "bootstrap.servers": cfg["kafka"]["brokers"],
            "group.id": cfg["kafka"]["group_id"],
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "error_cb": error_cb,
            "stats_cb": stats_cb,
            "statistics.interval.ms": 60000,
        })

        # 브로커 연결 확인: list_topics()로 metadata 요청
        try:
            cluster_meta = consumer.list_topics(timeout=KAFKA_CONNECT_TIMEOUT)
            broker_count = len(cluster_meta.brokers)
            log.info("Kafka 연결 OK: 브로커 %d대", broker_count)
            return consumer
        except KafkaException as e:
            consumer.close()
            log.warning("Kafka 브로커 연결 실패, %ds 후 재시도: %s",
                        KAFKA_CONNECT_RETRY, e)
            KAFKA_ERRORS.labels(type="connect_failed").inc()
            time.sleep(KAFKA_CONNECT_RETRY)


def safe_commit(kafka_consumer: Consumer):
    """Kafka 동기 커밋. 실패 시 로그만 남기고 계속 (다음 배치에서 재커밋)."""
    try:
        kafka_consumer.commit(asynchronous=False)
    except KafkaException as e:
        log.warning("Kafka commit 실패 (다음 배치에서 재시도): %s", e)
        KAFKA_COMMIT_ERRORS.inc()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = toml.load(config_path)

    level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True
    )

    # Redis 연결 (무한 재시도)
    r = connect_redis(cfg)
    log.info("Redis OK, pending: %d건", r.hlen("pending"))

    # Prometheus 메트릭 서버
    metrics_port = cfg.get("metrics", {}).get("port", 9101)
    start_http_server(metrics_port)
    log.info("Prometheus 메트릭: http://0.0.0.0:%d/metrics", metrics_port)

    # Kafka consumer (수동 커밋 + 에러 콜백 + 연결 확인)
    kafka_consumer = create_kafka_consumer(cfg)
    kafka_consumer.subscribe([cfg["kafka"]["topic"]])
    log.info("Kafka consumer 시작: topic=%s (수동 커밋)", cfg["kafka"]["topic"])

    processor = EventProcessor(r)

    running = True
    def sig_handler(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    last_log_count = 0
    last_metrics_time = time.time()
    batch_start_time = time.time()
    redis_down = False

    try:
        while running:
            msg = kafka_consumer.poll(1.0)

            # 10초마다 gauge 갱신
            now = time.time()
            if now - last_metrics_time >= 10:
                processor.update_metrics()
                last_metrics_time = now

            # Redis 다운 상태면 재연결 시도
            if redis_down:
                try:
                    r.ping()
                    redis_down = False
                    log.info("Redis 재연결 성공")
                except redis.exceptions.RedisError:
                    time.sleep(1)
                    continue  # poll은 했으므로 Kafka 연결은 유지

            if msg is None:
                # 타임아웃: 배치에 데이터 있으면 flush
                if processor.batch_size > 0 and \
                   now - batch_start_time >= BATCH_TIMEOUT:
                    if processor.flush_batch():
                        safe_commit(kafka_consumer)
                        batch_start_time = now
                    else:
                        redis_down = True
                        log.warning("Redis 장애 감지, 재연결 대기")
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka 메시지 에러: %s", msg.error())
                KAFKA_ERRORS.labels(type="message").inc()
                continue

            # 이벤트 파싱 (Redis 호출 없음)
            processor.parse_event(msg.value())

            # 배치 크기 도달 시 flush + commit
            if processor.batch_size >= BATCH_SIZE:
                if processor.flush_batch():
                    safe_commit(kafka_consumer)
                    batch_start_time = time.time()
                else:
                    redis_down = True
                    log.warning("Redis 장애 감지, 재연결 대기")

            # 1000건마다 로그
            if processor.stats["processed"] - last_log_count >= 1000:
                last_log_count = processor.stats["processed"]
                processor.update_metrics()
                last_metrics_time = time.time()
                log.info("이벤트: %d건, 삭제스킵: %d, pending: %s",
                         processor.stats["processed"],
                         processor.stats["skipped_delete"],
                         r.hlen("pending") if not redis_down else "N/A")
    finally:
        # 남은 배치 flush 시도
        if processor.batch_size > 0:
            if processor.flush_batch():
                safe_commit(kafka_consumer)
        kafka_consumer.close()
        log.info("종료. 총 이벤트: %d건", processor.stats["processed"])


if __name__ == "__main__":
    main()
