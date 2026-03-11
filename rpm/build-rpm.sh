#!/bin/bash
# file-tracker RPM 빌드 스크립트
# RHEL 9/10 또는 CentOS Stream 9/10에서 실행
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SPEC="$SCRIPT_DIR/file-tracker.spec"

echo "=== file-tracker RPM 빌드 ==="

# 1. 빌드 의존성 설치
echo "[1/4] 빌드 의존성 설치..."
sudo dnf install -y \
    rpm-build \
    cmake gcc-c++ \
    clang \
    libbpf-devel \
    bpftool \
    zlib-devel \
    2>&1 | grep -E "^(Installing|Already|Complete)" || true

# CRB 레포 활성화 (libbpf-devel이 여기 있음)
sudo dnf config-manager --set-enabled crb 2>/dev/null || \
sudo dnf config-manager --set-enabled CRB 2>/dev/null || true

# 2. librdkafka 빌드 (시스템에 없으면)
if ! pkg-config --exists rdkafka 2>/dev/null && [ ! -f /usr/lib/librdkafka.so ]; then
    echo "[2/4] librdkafka 빌드 (소스)..."
    RDKAFKA_VER="2.3.0"
    TMPDIR=$(mktemp -d)
    cd "$TMPDIR"
    curl -sL "https://github.com/confluentinc/librdkafka/archive/refs/tags/v${RDKAFKA_VER}.tar.gz" | tar xz
    cd "librdkafka-${RDKAFKA_VER}"
    ./configure --prefix=/usr
    make -j$(nproc)
    sudo make install
    sudo ldconfig
    cd /
    rm -rf "$TMPDIR"
else
    echo "[2/4] librdkafka 이미 설치됨, skip"
fi

# 3. rpmbuild 디렉토리 준비
echo "[3/4] rpmbuild 준비..."
mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# 소스를 BUILD 디렉토리에 복사
rm -rf ~/rpmbuild/BUILD/file-tracker
cp -a "$PROJECT_DIR" ~/rpmbuild/BUILD/file-tracker
rm -rf ~/rpmbuild/BUILD/file-tracker/build

# 4. RPM 빌드
echo "[4/4] RPM 빌드..."
export PKG_CONFIG_PATH=/usr/lib/pkgconfig:/usr/lib64/pkgconfig
rpmbuild -bb "$SPEC"

# 결과
RPM=$(find ~/rpmbuild/RPMS -name "file-tracker-*.rpm" -type f | head -1)
echo ""
echo "=== 빌드 완료 ==="
echo "RPM: $RPM"
echo "크기: $(du -h "$RPM" | cut -f1)"
echo ""
echo "설치: sudo rpm -ivh $RPM"
