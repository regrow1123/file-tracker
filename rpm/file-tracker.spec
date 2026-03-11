Name:           file-tracker
Version:        1.0.0
Release:        1%{?dist}
Summary:        eBPF-based real-time file change tracker
License:        GPLv2
URL:            https://github.com/regrow1123/file-tracker

BuildRequires:  clang >= 15
BuildRequires:  libbpf-devel >= 1.0
BuildRequires:  bpftool
BuildRequires:  cmake >= 3.16
BuildRequires:  gcc-c++
BuildRequires:  zlib-devel

Requires:       zlib
Requires:       libbpf

# librdkafka is built from source (not in RHEL repos), skip auto-detection
%global __requires_exclude librdkafka

%description
eBPF-based agent that monitors file deletions, modifications, and renames
under /home and sends events to Apache Kafka in real-time.
Uses CO-RE (Compile Once, Run Everywhere) for cross-kernel compatibility.

%prep
rm -rf %{_builddir}/file-tracker
cp -a /home/vagrant/file-tracker %{_builddir}/file-tracker

%build
cd %{_builddir}/file-tracker
rm -rf build && mkdir build && cd build
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_FLAGS="-O2"
make %{?_smp_mflags}

%install
rm -rf %{buildroot}

install -D -m 0755 %{_builddir}/file-tracker/build/file-tracker \
    %{buildroot}/usr/local/bin/file-tracker

install -D -m 0644 %{_builddir}/file-tracker/deploy/config.toml \
    %{buildroot}/etc/file-tracker/config.toml

install -D -m 0644 %{_builddir}/file-tracker/deploy/file-tracker.service \
    %{buildroot}/usr/lib/systemd/system/file-tracker.service

install -d -m 0755 %{buildroot}/var/lib/file-tracker

%pre
# Stop service before upgrade
if [ $1 -ge 2 ]; then
    systemctl stop file-tracker 2>/dev/null || true
fi

%post
systemctl daemon-reload
if [ $1 -eq 1 ]; then
    echo "file-tracker installed. Next steps:"
    echo "  1. Edit /etc/file-tracker/config.toml"
    echo "  2. systemctl enable --now file-tracker"
fi

%preun
# Stop and disable on uninstall (not upgrade)
if [ $1 -eq 0 ]; then
    systemctl stop file-tracker 2>/dev/null || true
    systemctl disable file-tracker 2>/dev/null || true
fi

%postun
systemctl daemon-reload
if [ $1 -eq 0 ]; then
    echo "file-tracker removed."
fi

%files
%attr(0755, root, root) /usr/local/bin/file-tracker
%config(noreplace) /etc/file-tracker/config.toml
/usr/lib/systemd/system/file-tracker.service
%dir /var/lib/file-tracker

%changelog
* Wed Mar 11 2026 regrow1123 - 1.0.0-1
- Initial release
- eBPF kprobes: vfs_unlink, vfs_write, do_truncate, vfs_utimes, vfs_rename
- Debounce (10s quiet, 1h max), BPF inode dedup
- Kafka producer with WAL for at-least-once delivery
- CO-RE support for RHEL 9 and RHEL 10
