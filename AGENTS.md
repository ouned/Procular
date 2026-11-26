# AGENTS.md

Procular is a GTK 4 / libadwaita process manager for GNOME, written in Python. It reads `/proc`,
`/sys`, systemd over D-Bus and NVML, and runs as the user; root actions go through a `pkexec`
helper.

Read before changing code:

- [ARCHITECTURE.md](ARCHITECTURE.md): threads, data flow, privilege elevation, and under
  **Conventions** the rules for records, `None` values, `/proc` parsing and a table of where each
  kind of change goes (column, tab, setting, highlight, privileged operation, GPU backend).
- [README.md](README.md) **Development**: source layout and every command below in more detail.

## Never open windows on the user's desktop

Anything that creates windows (UI tests, screenshots, trying out a change) runs in the off-screen
session, [tools/headless-session.sh](tools/headless-session.sh) (headless mutter, its own D-Bus
session bus). Do not start `./procular.sh` or `python3 -m procular` directly to check a change.

Unsetting `WAYLAND_DISPLAY` / `DISPLAY` is **not** headless: GTK falls back to
`$XDG_RUNTIME_DIR/wayland-0`, the real desktop. To test the no-display path, use
`GDK_BACKEND=x11` with `DISPLAY` unset.

## Commands

```sh
tools/headless-session.sh python3 -m pytest -q -p no:cacheprovider   # all tests, as CI runs them
tools/headless-session.sh python3 -m pytest -m ui                     # just the UI tests
python3 -m pytest tests/test_linux.py                                 # one unit-test file; UI tests skip
pipx run --spec ruff==0.17.6 ruff check .                             # ruff version is pinned, keep it
shellcheck install.sh procular.sh tools/*.sh pkg/*.sh
tools/headless-shot.sh /tmp/out.png "page:services"                   # render one view (actions: tools/shots.py)
```

UI tests ([tests/test_ui.py](tests/test_ui.py)) fail on any GTK or libadwaita warning and on
exceptions raised in callbacks, so a new warning is a test failure, not noise.

Slow or container-based, run only when the change calls for it: `tools/compat-test.sh` (tests
and screenshots on Ubuntu 24.04 in Podman), `tools/readme-shots.sh` (regenerates
`docs/screenshots/`, ~4 min), `pkg/build.sh deb|rpm`.

## Rules that are easy to break

- **Oldest supported stack: GTK 4.14, libadwaita 1.5, Python 3.12** (Ubuntu 24.04). CI also runs
  Debian 13 and the newest Fedora. Newer API only behind a version check (as `Adw.Spinner`) or
  replaced by an own widget (as [wrap_box.py](procular/widgets/wrap_box.py) for `Adw.WrapBox`).
- **Threads:** only the main thread touches widgets, models and signals. Workers return plain
  data; use `run_async(func, callback)` from [tasks.py](procular/util/tasks.py). Do not block the
  main loop with file I/O, D-Bus or subprocess calls.
- **The helper runs as root.** [procular_helper.py](procular/helper/procular_helper.py) is
  standard library only, with no shell. New root operations become an `op_*` function plus an
  `OPERATIONS` entry with strict input validation and tests in
  [tests/test_helper.py](tests/test_helper.py). The app itself never elevates.
- New `/proc` parsing goes into [util/linux.py](procular/util/linux.py) as a pure function, with
  a test.
- A value that could not be read is `None`, not 0. Never mutate a record after a worker returned
  it.
- Runtime dependencies are only PyGObject and psutil (from the distribution's packages). Do not
  add others without asking.

## Keep the docs in step

When a change moves files, adds a module, tab, setting or tool, or changes how the threads or the
helper work, update the matching part of [README.md](README.md) (feature list, source layout,
commands) and [ARCHITECTURE.md](ARCHITECTURE.md) in the same change. If a change alters a view
shown in the README, say so; regenerating the screenshots is a separate step.
