PROJECT_DIR := $(shell pwd)
SPEC         := $(PROJECT_DIR)/rpm/file-tracker.spec
RDKAFKA_VER  := 2.3.0
NPROC        := $(shell nproc)

.PHONY: all build rpm deps deps-rdkafka clean install uninstall

all: build

# --- 빌드 ---
build:
	@mkdir -p build && cd build && \
	cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-O2" && \
	make -j$(NPROC)

clean:
	rm -rf build

# --- 의존성 (RHEL/CentOS) ---
deps:
	sudo dnf install -y \
		rpm-build cmake gcc-c++ clang \
		libbpf-devel bpftool zlib-devel
	sudo dnf config-manager --set-enabled crb 2>/dev/null || \
	sudo dnf config-manager --set-enabled CRB 2>/dev/null || true

deps-rdkafka:
	@if pkg-config --exists rdkafka 2>/dev/null || [ -f /usr/lib/librdkafka.so ]; then \
		echo "librdkafka already installed, skip"; \
	else \
		echo "Building librdkafka $(RDKAFKA_VER) from source..." && \
		TMPDIR=$$(mktemp -d) && \
		cd $$TMPDIR && \
		curl -sL https://github.com/confluentinc/librdkafka/archive/refs/tags/v$(RDKAFKA_VER).tar.gz | tar xz && \
		cd librdkafka-$(RDKAFKA_VER) && \
		./configure --prefix=/usr && \
		make -j$(NPROC) && \
		sudo make install && \
		sudo ldconfig && \
		rm -rf $$TMPDIR; \
	fi

# --- RPM ---
rpm: build
	mkdir -p ~/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
	rm -rf ~/rpmbuild/BUILD/file-tracker
	cp -a $(PROJECT_DIR) ~/rpmbuild/BUILD/file-tracker
	rm -rf ~/rpmbuild/BUILD/file-tracker/build
	PKG_CONFIG_PATH=/usr/lib/pkgconfig:/usr/lib64/pkgconfig \
		rpmbuild -bb $(SPEC)
	@echo ""
	@echo "=== RPM ==="
	@find ~/rpmbuild/RPMS -name "file-tracker-*.rpm" -type f

# --- 로컬 설치/제거 (RPM 없이) ---
install: build
	sudo install -m 0755 build/file-tracker /usr/local/bin/file-tracker
	sudo mkdir -p /etc/file-tracker /var/lib/file-tracker
	@if [ ! -f /etc/file-tracker/config.toml ]; then \
		sudo install -m 0644 deploy/config.toml /etc/file-tracker/config.toml; \
	fi
	sudo install -m 0644 deploy/file-tracker.service /etc/systemd/system/file-tracker.service
	sudo systemctl daemon-reload
	@echo "Done. systemctl enable --now file-tracker"

uninstall:
	sudo systemctl stop file-tracker 2>/dev/null || true
	sudo systemctl disable file-tracker 2>/dev/null || true
	sudo rm -f /usr/local/bin/file-tracker
	sudo rm -f /etc/systemd/system/file-tracker.service
	sudo systemctl daemon-reload
	@echo "Removed. Config kept at /etc/file-tracker/"
