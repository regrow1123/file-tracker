#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/backup-consumer"
CONFIG_DIR="/etc/backup-consumer"

echo "=== backup-consumer 설치 ==="

# 의존성
echo "[1/5] Python 패키지 설치"
pip3 install confluent-kafka redis toml croniter

# 소스 복사
echo "[2/5] 소스 배포"
mkdir -p "$INSTALL_DIR"
cp consumer.py backup.py main.py cli.py "$INSTALL_DIR/"

# 설정
echo "[3/5] 설정 파일"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cp config.toml "$CONFIG_DIR/config.toml"
    echo "  → 설정 파일 생성됨. 수정 필요: $CONFIG_DIR/config.toml"
else
    echo "  → 기존 설정 유지: $CONFIG_DIR/config.toml"
fi

# systemd
echo "[4/5] systemd 등록"
cp deploy/backup-consumer.service /etc/systemd/system/
systemctl daemon-reload

# 사용자
echo "[5/5] 서비스 사용자"
id -u backup &>/dev/null || useradd -r -s /sbin/nologin backup

echo ""
echo "=== 설치 완료 ==="
echo "설정 수정: $CONFIG_DIR/config.toml"
echo "시작:      systemctl start backup-consumer"
echo "상태:      systemctl status backup-consumer"
echo "로그:      journalctl -u backup-consumer -f"
echo "CLI:       python3 $INSTALL_DIR/cli.py -c $CONFIG_DIR/config.toml status"
