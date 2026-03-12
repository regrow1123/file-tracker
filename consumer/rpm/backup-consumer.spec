Name:           backup-consumer
Version:        1.0.0
Release:        1%{?dist}
Summary:        Kafka → Redis file change event consumer
License:        GPLv2
URL:            https://github.com/regrow1123/file-tracker
BuildArch:      noarch

Requires:       python3 >= 3.9
Requires:       python3-pip

%description
Consumes file change events from Apache Kafka (produced by file-tracker agent)
and stores them in Redis pending Hash for downstream backup systems.

Features:
- Manual Kafka commit with batch Redis pipeline (at-least-once)
- Automatic Kafka/Redis reconnection on failure
- Prometheus metrics exporter (:9101/metrics)
- Horizontal scaling via Kafka consumer group

%prep
# Sources are pre-staged by build script

%install
rm -rf %{buildroot}

# Python source
install -d -m 0755 %{buildroot}/opt/backup-consumer
install -m 0644 %{_builddir}/backup-consumer/main.py \
    %{buildroot}/opt/backup-consumer/main.py
install -m 0644 %{_builddir}/backup-consumer/consumer.py \
    %{buildroot}/opt/backup-consumer/consumer.py
install -m 0644 %{_builddir}/backup-consumer/requirements.txt \
    %{buildroot}/opt/backup-consumer/requirements.txt

# Config
install -D -m 0644 %{_builddir}/backup-consumer/config.toml \
    %{buildroot}/etc/backup-consumer/config.toml

# systemd
install -D -m 0644 %{_builddir}/backup-consumer/deploy/backup-consumer.service \
    %{buildroot}/usr/lib/systemd/system/backup-consumer.service

%pre
# Create service user
getent group backup >/dev/null || groupadd -r backup
getent passwd backup >/dev/null || useradd -r -g backup -s /sbin/nologin backup

# Stop on upgrade
if [ $1 -ge 2 ]; then
    systemctl stop backup-consumer 2>/dev/null || true
fi

%post
# Install Python dependencies
pip3 install --quiet confluent-kafka redis toml prometheus_client 2>/dev/null || true
systemctl daemon-reload

if [ $1 -eq 1 ]; then
    echo ""
    echo "backup-consumer installed. Next steps:"
    echo "  1. Edit /etc/backup-consumer/config.toml"
    echo "     - Set [kafka] brokers"
    echo "     - Set [db] redis_url"
    echo "  2. systemctl enable --now backup-consumer"
    echo "  3. Verify: curl http://localhost:9101/metrics"
    echo ""
fi

%preun
if [ $1 -eq 0 ]; then
    systemctl stop backup-consumer 2>/dev/null || true
    systemctl disable backup-consumer 2>/dev/null || true
fi

%postun
systemctl daemon-reload

%files
%dir /opt/backup-consumer
/opt/backup-consumer/main.py
/opt/backup-consumer/consumer.py
/opt/backup-consumer/requirements.txt
%config(noreplace) /etc/backup-consumer/config.toml
/usr/lib/systemd/system/backup-consumer.service

%changelog
* Thu Mar 12 2026 regrow1123 - 1.0.0-1
- Initial release
- Manual Kafka commit + batch Redis pipeline (at-least-once)
- Kafka error callback + broker reconnection
- Redis reconnection with retry
- Prometheus exporter (:9101/metrics)
- Event processing: mtime_change → HSET, delete → skip, rename → pipeline
