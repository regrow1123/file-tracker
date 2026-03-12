#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONSUMER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== backup-consumer RPM 빌드 ==="

# rpmbuild 디렉토리 구조
mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# 소스 스테이징
BUILD_DIR=~/rpmbuild/BUILD/backup-consumer
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/deploy"

cp "$CONSUMER_DIR/main.py" "$BUILD_DIR/"
cp "$CONSUMER_DIR/consumer.py" "$BUILD_DIR/"
cp "$CONSUMER_DIR/config.toml" "$BUILD_DIR/"
cp "$CONSUMER_DIR/requirements.txt" "$BUILD_DIR/"
cp "$CONSUMER_DIR/deploy/backup-consumer.service" "$BUILD_DIR/deploy/"

# spec 복사
cp "$SCRIPT_DIR/backup-consumer.spec" ~/rpmbuild/SPECS/

# RPM 빌드
rpmbuild -bb ~/rpmbuild/SPECS/backup-consumer.spec

echo ""
echo "=== 빌드 완료 ==="
find ~/rpmbuild/RPMS -name "backup-consumer*.rpm" -exec echo "  {}" \;
