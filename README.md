# Procular

[![CI](https://github.com/ouned/Procular/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/ouned/Procular/actions/workflows/ci.yml?query=branch%3Amain)
[![CodeQL](https://github.com/ouned/Procular/actions/workflows/codeql.yml/badge.svg?branch=main&event=push)](https://github.com/ouned/Procular/actions/workflows/codeql.yml?query=branch%3Amain)
[![Release](https://github.com/ouned/Procular/actions/workflows/release.yml/badge.svg?event=push)](https://github.com/ouned/Procular/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

A process manager for administrators and developers on GNOME, inspired by
[System Informer](https://systeminformer.sourceforge.io/).

<a href="docs/screenshots/processes-dark.png"><img src="docs/screenshots/processes-dark.png" alt="The process tree sorted by CPU during a burst of compressors: the busiest branch first, heat-coloured CPU, GPU and I/O cells, rows tinted by user"></a>

## Personal Notes
- I always missed this kind of task manager, and now I have switched to Linux with GNOME, no more Windows.
- With AI speeding up coding, I finally just built it myself.
- This project is basically "vibe-coded", but it was not a one-shot prompt.
- The name and logo are also AI-generated, but I chose which ones to take.
- Built with Python, GTK 4 and libadwaita.
- I don't think Flatpak or Snap make much sense for this kind of tool, so I only added .deb and .rpm packages.
- It's Python, so there is nothing to compile anyway. You can also just use the install.sh script.

## Screenshots

Each view in the light and the dark style. Click a screenshot to open it at full size.

<table>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/properties-general-light.png"><img src="docs/screenshots/properties-general-light.png" alt="Process properties, General page: executable, command line, live CPU, memory, I/O and GPU tiles, process and control group details (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/properties-general-dark.png"><img src="docs/screenshots/properties-general-dark.png" alt="Process properties, General page: executable, command line, live CPU, memory, I/O and GPU tiles, process and control group details (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Process properties: overview of one process</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/properties-light.png"><img src="docs/screenshots/properties-light.png" alt="Process properties, Performance page: CPU, memory, I/O and GPU history of one process (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/properties-dark.png"><img src="docs/screenshots/properties-dark.png" alt="Process properties, Performance page: CPU, memory, I/O and GPU history of one process (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Process properties: performance history</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-summary-light.png"><img src="docs/screenshots/sysinfo-summary-light.png" alt="System Information summary: CPU, memory, GPU, disk and network graphs, CPU peaks labelled with the process responsible (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-summary-dark.png"><img src="docs/screenshots/sysinfo-summary-dark.png" alt="System Information summary: CPU, memory, GPU, disk and network graphs, CPU peaks labelled with the process responsible (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>System Information: peaks are labelled with the process behind them</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-cores-light.png"><img src="docs/screenshots/sysinfo-cores-light.png" alt="CPU page with one graph per logical CPU (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-cores-dark.png"><img src="docs/screenshots/sysinfo-cores-dark.png" alt="CPU page with one graph per logical CPU (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>One graph per CPU</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-gpu-light.png"><img src="docs/screenshots/sysinfo-gpu-light.png" alt="GPU page: graphics/compute and video codec utilisation, VRAM and shared memory, clocks, temperature and power (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-gpu-dark.png"><img src="docs/screenshots/sysinfo-gpu-dark.png" alt="GPU page: graphics/compute and video codec utilisation, VRAM and shared memory, clocks, temperature and power (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>GPU: engines, VRAM, clocks, temperature</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-network-light.png"><img src="docs/screenshots/sysinfo-network-light.png" alt="Network page: total throughput, per-interface throughput and Wi-Fi signal strength, addresses and link details (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-network-dark.png"><img src="docs/screenshots/sysinfo-network-dark.png" alt="Network page: total throughput, per-interface throughput and Wi-Fi signal strength, addresses and link details (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Network: per-interface throughput, Wi-Fi signal, link details</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-pressure-light.png"><img src="docs/screenshots/sysinfo-pressure-light.png" alt="Pressure page: CPU, memory and I/O pressure stall information over time (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/sysinfo-pressure-dark.png"><img src="docs/screenshots/sysinfo-pressure-dark.png" alt="Pressure page: CPU, memory and I/O pressure stall information over time (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Pressure: how long tasks stalled waiting for CPU, memory and I/O</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/services-light.png"><img src="docs/screenshots/services-light.png" alt="Services tab: systemd system and user units with state, process count, memory and CPU (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/services-dark.png"><img src="docs/screenshots/services-dark.png" alt="Services tab: systemd system and user units with state, process count, memory and CPU (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Services: systemd units with their resource use</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/history-light.png"><img src="docs/screenshots/history-light.png" alt="History tab listing exited processes with run time, peak CPU and memory, parent and command line (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/history-dark.png"><img src="docs/screenshots/history-dark.png" alt="History tab listing exited processes with run time, peak CPU and memory, parent and command line (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>History: processes that exited while Procular was running</sub></td>
  </tr>
  <tr>
    <td width="50%">
      <a href="docs/screenshots/devices-light.png"><img src="docs/screenshots/devices-light.png" alt="Devices tab: the device tree below the computer with status, runtime power state, driver, type, interfaces, the processes using each device, link speed, interrupt rate, vendor and address (light)"></a>
    </td>
    <td width="50%">
      <a href="docs/screenshots/devices-dark.png"><img src="docs/screenshots/devices-dark.png" alt="Devices tab: the device tree below the computer with status, runtime power state, driver, type, interfaces, the processes using each device, link speed, interrupt rate, vendor and address (dark)"></a>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center"><sub>Devices: the device tree with drivers, power state and who uses each device</sub></td>
  </tr>
</table>

## Features

### Processes

- **Process tree**: live parent/child tree; sorting by a resource puts the busiest branch first.
- **40 columns**: CPU and memory sparklines, GPU, private bytes, working set, swap, I/O, OOM score,
  systemd unit, container, … (right-click a header). CPU, memory and I/O cells are heat-coloured.
- **Highlighting**: rows coloured by user or by category (container, Flatpak/Snap, service, kernel
  thread, …). New, exited, suspended and zombie processes are always marked.
- **Actions**: kill (also the whole tree), suspend/resume, send any signal, set priority, I/O
  priority, CPU affinity and OOM adjustment, restart the owning systemd unit.
- **Properties**: paths, cgroup, capabilities, security labels, namespaces, performance graphs,
  threads, modules, memory regions, open files, network, environment, limits.
- **Find Open Files** (Ctrl+Shift+F): who holds a file, library, port or device open, or keeps a
  deleted file alive. Answers "why won't this disk unmount?".
- **Administrator mode**: act on and read other users' processes through a strictly validated
  helper (pkexec).

### Tabs

- **Services**: systemd system and user units: start, stop, restart, enable, disable, journal,
  per-unit CPU, memory and pressure.
- **Network**: TCP/UDP sockets with owning process, per-connection rates, reverse DNS, close
  connection, whois.
- **Disk**: read/write rates per process and per device, active time, mount points.
- **History**: processes that exited while Procular ran, with run time, peak CPU/memory and exit
  status.
- **Devices**: hardware tree by connection (PCI, USB hub, port) with driver, link speed,
  interrupts, power draw, time suspended and the processes using each device. Updates on hotplug.
  Double-click for IDs, drivers that would support it, power graph, IOMMU group and all udev
  properties.

### System

- **System Information**: CPU per core, memory, disks (temperature, latency, LUKS/LVM/RAID), network,
  GPUs (AMD, NVIDIA, Intel, Arm; idle GPUs aren't woken) and pressure stall info. Hover a graph
  to see the top process at that moment.
- **Header and status bar**: CPU, memory and I/O mini graphs; process and thread counts.
- **Explanations** (F1): what the less obvious numbers on the current tab mean.
- **Refresh**: 0.5–10 s, F5 refresh, F6 pause; slows to 5 s when no window is visible.

## Installing a package

Each [release](https://github.com/ouned/Procular/releases) has a `.deb` (Ubuntu 24.04 or newer,
Debian 13 or newer) and an `.rpm` (Fedora). They pull in all dependencies and include the privileged
helper and its polkit policy:

```sh
sudo apt install ./procular_*_all.deb       # Ubuntu, Debian
sudo dnf install ./procular-*.noarch.rpm    # Fedora
```

To build them yourself (in Podman containers, so from any distribution):
`pkg/build.sh deb` and `pkg/build.sh rpm` write to `dist/`, and
`pkg/smoke-test.sh dist/procular_*.deb docker.io/library/debian:13` test-installs one.

## Requirements

To run from source or with `install.sh`:

- Ubuntu/Debian: `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil`
- Fedora: `sudo dnf install python3-gobject gtk4 libadwaita python3-psutil`

GTK ≥ 4.12, libadwaita ≥ 1.5, Python ≥ 3.12 (e.g. Ubuntu 24.04, Debian 13, Fedora 40 or newer).
NVIDIA GPUs are read through NVML (`libnvidia-ml.so.1`, part of the proprietary driver); without it they don't show up.

## Running

```sh
./procular.sh                     # from the source tree
./install.sh                      # install for your user (~/.local), adds an app-grid entry
sudo ./install.sh --helper        # optional: root-owned helper + polkit policy (remembers authentication briefly)
sudo ./install.sh --remove-helper # remove the helper and polkit policy
./install.sh --uninstall          # remove the user installation
```

If the helper isn't installed, Procular runs the bundled helper with `pkexec python3 -I …`
and asks for your password on every privileged action.

## Keyboard shortcuts

| Key | Action |
|---|---|
| F1 | Explain the values of the visible tab |
| Ctrl+K / type | Filter (name, PID, user, command line, unit, container) |
| Ctrl+T | Toggle tree view |
| Enter / double-click | Properties |
| Delete / Shift+Delete | Terminate / Kill |
| Ctrl+C | Copy row |
| F5 / F6 | Refresh now / Pause |
| Ctrl+I | System Information |
| Ctrl+Shift+F | Find open files |
| Alt+1…6 | Processes / Services / Network / Disk / History / Devices |
| Ctrl+, | Preferences |
| Ctrl+Q | Quit |

## Development

How the pieces fit together (threads, data flow, privilege elevation) is described in
[ARCHITECTURE.md](ARCHITECTURE.md).

### Source layout

| Path | What lives there |
| --- | --- |
| [procular/](procular/) | `app.py` (application, shared state, actions), `config.py` (settings), `style.css` / `style-light.css` |
| [procular/providers/](procular/providers/) | Data sources, run in worker threads. `processes.py` is the fast `/proc` scanner, `system.py` the system-wide counters and their history, `sampler.py` drives both on every tick. `gpu.py` (amdgpu / NVML / DRM fdinfo backends), `diskinfo.py`, `netinfo.py`, `network.py` (sockets and their owners), `services.py` (systemd over D-Bus), `devices.py` (hardware tree and hotplug), `handles.py` (open-file search index), `pci.py` |
| [procular/models/](procular/models/) | `process_tree.py` (one stable GObject per process, updated in place), `highlight.py` (row colour rules), `process_history.py` (exit records) |
| [procular/widgets/](procular/widgets/) | `process_view.py` (the process tree), `table.py` (generic live table behind the other tabs), the Services / Network / Disk / History / Devices views, `graph.py`, `sparkline.py`, `mini_graphs.py`, `process_actions.py`, `color_menu.py`, `info_menu.py`, `wrap_box.py`, `logo_animation.py` |
| [procular/windows/](procular/windows/) | `main_window.py`, `properties.py`, `device_properties.py`, `find_files.py`, `sysinfo.py`, `preferences.py`, `about.py` |
| [procular/helper/](procular/helper/) | `procular_helper.py`: the privileged helper run through pkexec. Standard library only, every operation whitelisted and validated |
| [procular/util/](procular/util/) | `linux.py` (pure `/proc` and cgroup parsers), `privileged.py` (calls the helper), `tasks.py` (thread pool → main loop), `format.py`, `timeline.py`, `icons.py`, `lifecycle.py`, `process_name.py`, `chiptune.py` + `audio.py` (the About tune) |
| [data/](data/) | Desktop file, AppStream metainfo, polkit policy, icons |
| [tests/](tests/) | pytest suite |
| [tools/](tools/) | Off-screen session, screenshots, compatibility test, demo load |
| [pkg/](pkg/) | `.deb` and `.rpm` packaging: `build.sh`, `smoke-test.sh`, `debian/`, `procular.spec`, `pkgversion.py` |

### Tests

The tests need the same packages as running from source (see [Requirements](#requirements)) plus
pytest (`python3-pytest`). The UI tests ([tests/test_ui.py](tests/test_ui.py)) drive the real app and
run only in an off-screen Wayland session, which needs `mutter` and `dbus-run-session` (package
`dbus-daemon`); elsewhere they are skipped, so they never open windows on your desktop.

```sh
python3 -m pytest                                   # parsers, providers, tree model, highlight rules, ...
tools/headless-session.sh python3 -m pytest         # the same plus the UI tests, off-screen (as CI runs it):
                                                    # spawn, select, suspend, renice, search, properties, kill,
                                                    # multi-select, services, network, disk, devices, sysinfo
tools/headless-session.sh python3 -m pytest -m ui   # just the UI tests
tools/compat-test.sh                                # all of the above on the oldest supported stack (Ubuntu 24.04, Podman)
```

### Screenshots

```sh
tools/headless-shot.sh out.png "page:services"   # render one view to PNG (actions: see tools/shots.py)
tools/readme-shots.sh                            # the README screenshots, light and dark, under a demo load (~4 min)
```

### Linting

CI pins ruff; the rule set is in `pyproject.toml`.

```sh
pipx run --spec ruff==0.17.6 ruff check .
shellcheck install.sh procular.sh tools/*.sh pkg/*.sh
```

### Packages

```sh
pkg/build.sh deb                 # .deb into dist/, built in Podman on Ubuntu 24.04
pkg/build.sh rpm                 # .rpm into dist/, built in Podman on Fedora
pkg/smoke-test.sh dist/procular_<version>_all.deb docker.io/library/debian:13   # install, check, remove
```

Set `NATIVE=1` to build without Podman; the build tools are listed at the top of `pkg/build.sh`.

### CI and releases

Workflows live in [.github/workflows/](.github/workflows/):

- **`ci.yml`**: lints (ruff, shellcheck, desktop file, polkit policy, AppStream metainfo), runs pytest on
  Ubuntu 24.04, Debian 13 and Fedora, runs the UI tests on an Ubuntu 24.04 runner, and builds the .deb and .rpm and
  test-installs them on Ubuntu 24.04, Debian 13 and Fedora 43 and 45.
- **`codeql.yml`**: scans the code, above all the privileged helper.
- **`compat-screenshots.yml`**: runs `tools/compat-test.sh` when started by hand or when a pull
  request gets the `compat-screenshots` label.
- **`release.yml`**: runs on a version tag. It runs CI again, builds the sdist, wheel, .deb and
  .rpm, and creates the GitHub release with them attached.

To release:

```sh
git tag -a v1.2.3 -m "Procular 1.2.3" && git push origin v1.2.3
```

## Settings

Settings are stored in `~/.config/procular/settings.json` (`PROCULAR_CONFIG` overrides the path). Besides the preferences this covers the
window size, the columns of the process list (which, order, widths), the sort column of every list and the
options of the Services and Network tabs.

## Security

Please report vulnerabilities privately, not as an issue. How, and what is in scope, is in
[SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
