# Built by pkg/build.sh, which passes the version: rpmbuild --define "pkg_version 1.2.3"
Name:           procular
Version:        %{pkg_version}
Release:        1%{?dist}
Summary:        Process manager for administrators and developers
License:        MIT
URL:            https://github.com/ouned/Procular
Source0:        procular.tar.gz
BuildArch:      noarch

BuildRequires:  make
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils

Requires:       python3 >= 3.12
Requires:       python3-gobject
Requires:       python3-psutil
Requires:       gtk4 >= 4.12
Requires:       libadwaita >= 1.5
Requires:       hicolor-icon-theme
# pkexec, for actions on other users' processes
Recommends:     polkit
# ss, for closing connections
Recommends:     iproute
# pw-play, for the About dialog's music
Suggests:       pipewire-utils

%description
Procular is a process manager for GNOME, inspired by System Informer. It shows
a live process tree with detailed per-process properties (threads, memory
regions, open files, sockets, environment), systemd services with their
journal, network connections, disk activity, exited processes and system
information for CPU, memory, disks, network interfaces and GPUs.

Actions on other users' processes go through a small, strictly validated
helper run with pkexec.

%prep
%autosetup -n procular

%build
# nothing to build

%install
%make_install PREFIX=%{_prefix} LIBEXECDIR=%{_libexecdir}
%py_byte_compile %{python3} %{buildroot}%{_datadir}/procular

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.ouned.Procular.desktop

%files
%license LICENSE
%doc README.md
%{_bindir}/procular
%{_datadir}/procular/
%{_libexecdir}/procular-helper
%{_datadir}/polkit-1/actions/io.github.ouned.Procular.policy
%{_datadir}/applications/io.github.ouned.Procular.desktop
%{_metainfodir}/io.github.ouned.Procular.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.ouned.Procular.svg

%changelog
* %(LC_ALL=C date -u -d @${SOURCE_DATE_EPOCH:-$(date +%%s)} "+%%a %%b %%d %%Y") Michael Losert <michael@losert.org> - %{version}-%{release}
- Built from the upstream release
