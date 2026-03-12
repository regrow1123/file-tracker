#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/backup-consumer"
CONFIG_DIR="/etc/backup-consumer"

echo "=== backup-consumer 설치 ==="

# 의존성
echo "[1/6] Python 패키지 설치"
pip3 install confluent-kafka redis toml minio

# 소스 복사
echo "[2/6] 소스 배포"
mkdir -p "$INSTALL_DIR"
cp consumer.py backup.py main.py prune.py cli.py "$INSTALL_DIR/"

# 설정
echo "[3/6] 설정 파일"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cp config.toml "$CONFIG_DIR/config.toml"
    echo "  → 설정 파일 생성됨. 수정 필요: $CONFIG_DIR/config.toml"
else
    echo "  → 기존 설정 유지: $CONFIG_DIR/config.toml"
fi

# 시크릿
if [ ! -f "$CONFIG_DIR/env" ]; then
    cp deploy/env.example "$CONFIG_DIR/env"
    chmod 600 "$CONFIG_DIR/env"
    chown root:root "$CONFIG_DIR/env"
    echo "  → 시크릿 파일 생성됨. 수정 필요: $CONFIG_DIR/env"
else
    echo "  → 기존 시크릿 유지: $CONFIG_DIR/env"
fi

# systemd
echo "[4/6] systemd 등록"
cp deploy/backup-consumer.service /etc/systemd/system/
cp deploy/backup-run.service /etc/systemd/system/
cp deploy/backup-run.timer /etc/systemd/system/
cp deploy/backup-prune.service /etc/systemd/system/
cp deploy/backup-prune.timer /etc/systemd/system/
systemctl daemon-reload

# 사용자
echo "[5/6] 서비스 사용자"
id -u backup &>/dev/null || useradd -r -s /sbin/nologin backup

echo "[6/6] 완료"
echo ""
echo "=== 설치 완료 ==="
echo "시크릿 수정: $CONFIG_DIR/env"
echo "설정 수정:   $CONFIG_DIR/config.toml"
echo ""
echo "서비스 시작:"
echo "  systemctl start backup-consumer        # Kafka → Redis (상시)"
echo "  systemctl enable --now backup-run.timer   # 매일 03:00 백업"
echo "  systemctl enable --now backup-prune.timer # 매주 일 04:00 prune"
echo ""
echo "상태 확인:"
echo "  systemctl list-timers backup-*"
echo "  journalctl -u backup-consumer -f"
echo "  journalctl -u backup-run -f"
