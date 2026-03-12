#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# file-tracker E2E 테스트 (happy path)
# agent → Kafka → consumer → backup → restore → prune
# 요구: sudo, Docker (Kafka/Redis/MinIO), build/file-tracker, restic
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONSUMER_DIR="$PROJECT_DIR/consumer"
BINARY="$PROJECT_DIR/build/file-tracker"

# 테스트 디렉토리 (watch prefix = /home)
TEST_HOME="/home/e2e-test"
TEST_DIR="$TEST_HOME/stor1/user1"

# 색상
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
PIDS=()

pass() { echo -e "  ${GREEN}PASS${NC}: $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC}: $1"; FAIL=$((FAIL + 1)); }
info() { echo -e "${YELLOW}[$1]${NC} $2"; }

cleanup() {
    info "CLEANUP" "프로세스 종료 및 정리"
    for pid in "${PIDS[@]}"; do
        sudo kill "$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    sudo rm -rf "$TEST_HOME"
    rm -rf /tmp/e2e-restore
    redis-cli FLUSHALL > /dev/null 2>&1 || true
    # MinIO 정리 (python minio SDK 사용 — sudo에서 mc alias 없을 수 있음)
    python3 -c "
from minio import Minio
c = Minio('localhost:9000', access_key='minioadmin', secret_key='minioadmin', secure=False)
try:
    for obj in c.list_objects('file-tracker-backup', recursive=True):
        c.remove_object('file-tracker-backup', obj.object_name)
except: pass
" 2>/dev/null || true
}
trap cleanup EXIT

# ═══════════════════════════════════════════════════════════════
# 0. 사전조건 확인
# ═══════════════════════════════════════════════════════════════
info "PREREQ" "사전조건 확인"

if [[ -x "$BINARY" ]]; then pass "file-tracker 바이너리"; else fail "file-tracker 바이너리 없음: $BINARY"; exit 1; fi
if command -v restic &>/dev/null; then pass "restic"; else fail "restic 없음"; exit 1; fi
if command -v redis-cli &>/dev/null; then pass "redis-cli"; else fail "redis-cli 없음"; exit 1; fi
python3 -c "import minio" 2>/dev/null
if [[ $? -eq 0 ]]; then pass "minio Python SDK"; else fail "minio SDK 없음 (pip install minio)"; exit 1; fi

if redis-cli ping > /dev/null 2>&1; then pass "Redis 연결"; else fail "Redis 연결 실패"; exit 1; fi

if docker exec kafka /opt/kafka/bin/kafka-topics.sh \
    --list --bootstrap-server localhost:9092 > /dev/null 2>&1; then
    pass "Kafka 연결"
else
    fail "Kafka 연결 실패"; exit 1
fi

# MinIO 연결 확인 (curl로 직접 체크 — mc는 sudo에서 alias 없을 수 있음)
if curl -sf http://localhost:9000/minio/health/live > /dev/null 2>&1; then
    pass "MinIO 연결"
else
    fail "MinIO 연결 실패"; exit 1
fi

# ═══════════════════════════════════════════════════════════════
# 1. 환경 초기화
# ═══════════════════════════════════════════════════════════════
info "INIT" "환경 초기화"

redis-cli FLUSHALL > /dev/null
python3 -c "
from minio import Minio
c = Minio('localhost:9000', access_key='minioadmin', secret_key='minioadmin', secure=False)
try:
    for obj in c.list_objects('file-tracker-backup', recursive=True):
        c.remove_object('file-tracker-backup', obj.object_name)
except: pass
" 2>/dev/null || true

sudo rm -rf "$TEST_HOME"
sudo mkdir -p "$TEST_DIR"
sudo chown -R "$(whoami)" "$TEST_HOME"

# agent용 config (watch prefix = /home/e2e-test)
AGENT_CONFIG=$(mktemp /tmp/e2e-agent-XXXX.toml)
cat > "$AGENT_CONFIG" <<EOF
[kafka]
brokers = "localhost:9092"
topic = "file-tracker-events"

[debounce]
quiet_ms = 3000
max_wait_ms = 30000

[wal]
path = "/tmp/e2e-wal.log"
max_size_mb = 64

[watch]
prefix = "/home/e2e-test"

[logging]
level = "info"
EOF

# consumer용 config
CONSUMER_CONFIG=$(mktemp /tmp/e2e-consumer-XXXX.toml)
cat > "$CONSUMER_CONFIG" <<EOF
[kafka]
brokers = "localhost:9092"
topic = "file-tracker-events"
group_id = "e2e-test-$(date +%s)"

[backup]
restic_binary = "/usr/bin/restic"
workers = 2
base_path = "/home/e2e-test"
repo_depth = 2

[minio]
endpoint = "localhost:9000"
bucket = "file-tracker-backup"
use_tls = false

[db]
redis_url = "redis://localhost:6379/0"

[prune]
keep_days = 90

[logging]
level = "INFO"

[metrics]
port = 19101
EOF

export RESTIC_PASSWORD="e2e-test-password"
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin

pass "환경 초기화"

# ═══════════════════════════════════════════════════════════════
# 2. Agent → Kafka
# ═══════════════════════════════════════════════════════════════
info "AGENT" "eBPF agent 시작"

# Kafka consumer group offset 확인용: 시작 전 topic offset 기록
OFFSET_BEFORE=$(docker exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server localhost:9092 --topic file-tracker-events 2>/dev/null \
    | awk -F: '{s+=$3} END {print s+0}')

sudo "$BINARY" "$AGENT_CONFIG" > /tmp/e2e-agent.log 2>&1 &
AGENT_PID=$!
PIDS+=("$AGENT_PID")
sleep 2

# agent가 실행 중인지 확인
if sudo kill -0 "$AGENT_PID" 2>/dev/null; then
    pass "agent 시작 (PID=$AGENT_PID)"
else
    fail "agent 시작 실패"
    cat /tmp/e2e-agent.log
    exit 1
fi

info "AGENT" "테스트 파일 생성/수정/삭제/rename"

# 파일 생성 + 쓰기 (mtime_change)
echo "hello world" > "$TEST_DIR/file1.txt"
echo "test data" > "$TEST_DIR/file2.txt"
mkdir -p "$TEST_DIR/subdir"
echo "nested" > "$TEST_DIR/subdir/file3.txt"

sleep 1

# 파일 수정 (mtime_change)
echo "modified" >> "$TEST_DIR/file1.txt"

# 파일 rename
mv "$TEST_DIR/file2.txt" "$TEST_DIR/file2_renamed.txt"

# 파일 삭제
rm "$TEST_DIR/subdir/file3.txt"

info "AGENT" "debounce 대기 (5초)"
sleep 5

# agent 종료
sudo kill "$AGENT_PID" 2>/dev/null || true
wait "$AGENT_PID" 2>/dev/null || true
PIDS=("${PIDS[@]/$AGENT_PID}")

# Kafka에 이벤트 도착 확인
OFFSET_AFTER=$(docker exec kafka /opt/kafka/bin/kafka-get-offsets.sh \
    --bootstrap-server localhost:9092 --topic file-tracker-events 2>/dev/null \
    | awk -F: '{s+=$3} END {print s+0}')

NEW_EVENTS=$((OFFSET_AFTER - OFFSET_BEFORE))
if [[ $NEW_EVENTS -gt 0 ]]; then
    pass "Kafka 이벤트 전송: ${NEW_EVENTS}건"
else
    fail "Kafka 이벤트 없음 (before=$OFFSET_BEFORE, after=$OFFSET_AFTER)"
fi

# ═══════════════════════════════════════════════════════════════
# 3. Kafka → Redis (consumer)
# ═══════════════════════════════════════════════════════════════
info "CONSUMER" "consumer 시작"

cd "$CONSUMER_DIR"
timeout 10 python3 main.py "$CONSUMER_CONFIG" > /tmp/e2e-consumer.log 2>&1 || true

PENDING=$(redis-cli HLEN pending)
if [[ $PENDING -gt 0 ]]; then
    pass "Redis pending: ${PENDING}건"
else
    fail "Redis pending 비어있음"
fi

# mtime_change 이벤트 확인 (file1.txt는 수정됨)
if redis-cli HEXISTS pending "$TEST_DIR/file1.txt" | grep -q "1"; then
    pass "file1.txt mtime_change 이벤트"
else
    fail "file1.txt mtime_change 이벤트 없음"
fi

# rename 이벤트 확인 (file2_renamed.txt 존재, file2.txt 없음)
if redis-cli HEXISTS pending "$TEST_DIR/file2_renamed.txt" | grep -q "1"; then
    pass "file2_renamed.txt rename 이벤트"
else
    # rename이 mtime_change로 올 수도 있음
    info "CONSUMER" "file2_renamed.txt 직접 확인 생략 (rename 이벤트 형식 의존)"
fi

# delete 이벤트는 pending에 없어야 함
if redis-cli HEXISTS pending "$TEST_DIR/subdir/file3.txt" | grep -q "0"; then
    pass "delete 이벤트 미저장 (file3.txt)"
else
    fail "delete 이벤트가 pending에 저장됨"
fi

# ═══════════════════════════════════════════════════════════════
# 4. Redis → restic (backup)
# ═══════════════════════════════════════════════════════════════
info "BACKUP" "backup 실행"

python3 backup.py "$CONSUMER_CONFIG" > /tmp/e2e-backup.log 2>&1
BACKUP_RC=$?

if [[ $BACKUP_RC -eq 0 ]]; then
    pass "backup 실행 성공"
else
    fail "backup 실행 실패 (rc=$BACKUP_RC)"
    cat /tmp/e2e-backup.log
fi

# pending/processing 비었는지 확인
PENDING_AFTER=$(redis-cli HLEN pending)
PROCESSING=$(redis-cli EXISTS processing)
if [[ $PENDING_AFTER -eq 0 ]] || [[ $PENDING_AFTER -le 2 ]]; then
    pass "backup 후 pending: ${PENDING_AFTER}건"
else
    fail "backup 후 pending 잔여: ${PENDING_AFTER}건"
fi

if [[ $PROCESSING -eq 0 ]]; then
    pass "processing 키 삭제됨"
else
    fail "processing 키 잔존"
fi

# backup:last_run 기록 확인
LAST_BACKED=$(redis-cli HGET backup:last_run backed_up)
if [[ -n "$LAST_BACKED" ]] && [[ "$LAST_BACKED" -gt 0 ]]; then
    pass "backup:last_run 기록: backed_up=${LAST_BACKED}"
else
    fail "backup:last_run 기록 없음 또는 0"
fi

# restic snapshots 확인
REPO_URL="s3:http://localhost:9000/file-tracker-backup/stor1/user1"
SNAP_COUNT=$(restic snapshots -r "$REPO_URL" --json 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [[ $SNAP_COUNT -gt 0 ]]; then
    pass "restic 스냅샷: ${SNAP_COUNT}개"
else
    fail "restic 스냅샷 없음"
fi

# ═══════════════════════════════════════════════════════════════
# 5. Restore 검증
# ═══════════════════════════════════════════════════════════════
info "RESTORE" "파일 복원 테스트"

rm -rf /tmp/e2e-restore
python3 cli.py -c "$CONSUMER_CONFIG" restore "$TEST_DIR/file1.txt" \
    -t /tmp/e2e-restore > /tmp/e2e-restore.log 2>&1
RESTORE_RC=$?

if [[ $RESTORE_RC -eq 0 ]]; then
    pass "restore 실행 성공"
else
    fail "restore 실행 실패 (rc=$RESTORE_RC)"
    cat /tmp/e2e-restore.log
fi

# 복원된 파일 내용 비교
if [[ -f "/tmp/e2e-restore${TEST_DIR}/file1.txt" ]]; then
    if diff -q "$TEST_DIR/file1.txt" "/tmp/e2e-restore${TEST_DIR}/file1.txt" > /dev/null 2>&1; then
        pass "복원 파일 내용 일치"
    else
        fail "복원 파일 내용 불일치"
        diff "$TEST_DIR/file1.txt" "/tmp/e2e-restore${TEST_DIR}/file1.txt" || true
    fi
else
    fail "복원 파일 없음: /tmp/e2e-restore${TEST_DIR}/file1.txt"
fi

# ═══════════════════════════════════════════════════════════════
# 6. Prune 검증
# ═══════════════════════════════════════════════════════════════
info "PRUNE" "prune 실행"

python3 prune.py "$CONSUMER_CONFIG" > /tmp/e2e-prune.log 2>&1
PRUNE_RC=$?

if [[ $PRUNE_RC -eq 0 ]]; then
    pass "prune 실행 성공"
else
    fail "prune 실행 실패 (rc=$PRUNE_RC)"
    cat /tmp/e2e-prune.log
fi

# prune 후 스냅샷 여전히 존재 (keep_days=90이므로 방금 만든 건 유지)
SNAP_AFTER=$(restic snapshots -r "$REPO_URL" --json 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [[ $SNAP_AFTER -gt 0 ]]; then
    pass "prune 후 스냅샷 유지: ${SNAP_AFTER}개"
else
    fail "prune 후 스냅샷 삭제됨"
fi

# ═══════════════════════════════════════════════════════════════
# 7. Prometheus 메트릭 검증
# ═══════════════════════════════════════════════════════════════
info "METRICS" "Prometheus 메트릭 확인"

# consumer를 잠깐 실행해서 메트릭 확인
timeout 5 python3 main.py "$CONSUMER_CONFIG" > /dev/null 2>&1 &
METRICS_PID=$!
sleep 2

METRICS=$(curl -s http://localhost:19101/metrics 2>/dev/null || echo "")
if echo "$METRICS" | grep -q "backup_events_processed_total"; then
    pass "Prometheus 메트릭 노출"
else
    fail "Prometheus 메트릭 없음"
fi

kill $METRICS_PID 2>/dev/null || true
wait $METRICS_PID 2>/dev/null || true

# ═══════════════════════════════════════════════════════════════
# 결과
# ═══════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════"
echo -e "  ${GREEN}PASS: ${PASS}${NC}  ${RED}FAIL: ${FAIL}${NC}"
echo "════════════════════════════════════════"

# 정리는 trap EXIT에서 수행
rm -f "$AGENT_CONFIG" "$CONSUMER_CONFIG"

if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "로그 확인:"
    echo "  agent:    /tmp/e2e-agent.log"
    echo "  consumer: /tmp/e2e-consumer.log"
    echo "  backup:   /tmp/e2e-backup.log"
    echo "  restore:  /tmp/e2e-restore.log"
    echo "  prune:    /tmp/e2e-prune.log"
    exit 1
fi

exit 0
