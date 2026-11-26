"""Persistent JSON settings (~/.config/procular/settings.json)."""

import contextlib
import copy
import json
import os
import re
import sys
import tempfile

from gi.repository import GLib, GObject

from ._version import get_version

APP_ID = "io.github.ouned.Procular"
APP_NAME = "Procular"
VERSION = get_version()

# (key, label, description, default color, enabled by default)
HIGHLIGHTS = [
    ("new", "New processes", "Processes that just started", "#2ec27e", True),
    ("term", "Terminated processes", "Processes that just exited", "#e01b24", True),
    ("stopped", "Suspended processes", "Stopped with SIGSTOP or traced", "#77767b", True),
    ("zombie", "Zombie processes", "Exited but not reaped by their parent", "#9141ac", True),
    ("container", "Containers", "Docker, Podman, LXC, systemd-nspawn, Kubernetes", "#c061cb", True),
    ("sandboxed", "Sandboxed apps", "Flatpak and Snap applications", "#dc8add", True),
    ("system_service", "System services", "Processes owned by a systemd system service", "#62a0ea", True),
    ("elevated", "Elevated processes", "setuid or extra capabilities for a non-root user", "#ff7800", True),
    ("user_service", "User services", "Processes owned by a systemd user service", "#33c7de", True),
    ("kthread", "Kernel threads", "Threads created by the kernel (kthreadd)", "#9a9996", True),
    ("own", "Own processes", "Processes running as the current user", "#f6d32d", True),
    ("high_priority", "High priority", "Processes with a negative nice value", "#ffa348", False),
    ("other_user", "Other users", "Processes owned by root or other users", "#deddda", False),
]

DEFAULTS = {
    "color_scheme": "system",  # "system" (follow the desktop), "light" or "dark"
    "interval_ms": 1000,
    "background_slowdown": True,  # update less often while no window is visible
    "highlight_seconds": 1,
    "confirm_kill": True,
    "show_kernel_threads": False,
    "show_other_users": True,
    "tree_mode": True,
    "highlight_mode": "user",  # what steady row colours mean: "user", "category" or "none"
    "user_colors": {},  # str(uid) -> colour, where the user picked one (highlight_mode "user")
    "highlight_alpha": 0.35,
    "heat_cells": True,
    "binary_names": False,  # name processes after their program file, not what they call themselves
    "highlights": {key: {"color": color, "enabled": enabled} for key, _, _, color, enabled in HIGHLIGHTS},
    "columns": ["name", "pid", "cpu", "gpu", "private", "rss", "io_total", "user", "cmdline"],
    "column_widths": {},
    # list name ("processes", "services", ...) -> {"column": id, "descending": bool}, or None for
    # a list the user left unsorted; lists that are not in here use their built-in order
    "sort": {},
    "window": {"width": 1280, "height": 800, "maximized": False},
    "services_scope": "both",
    "services_only": True,
    "network_show_listening": True,
    "network_resolve_dns": True,
    "devices_show_all": False,  # Devices tab: also virtual devices, ACPI objects, the parts of devices
    "sysinfo_per_core": False,
    "about_music": True,  # the About dialog plays its theme
    "about_volume": 0.5,  # position of its volume slider, 0..1
    "about_track": "hyperthread",  # which of chiptune.TRACKS it plays
    "history_samples": 1200,
    "exit_history_size": 2000,
}


# What a stored number may be: outside of it the nearest limit is used. A value from a file
# edited by hand must not stop the application from starting (a history of -1 samples) or make
# it spin (an interval of 0 ms).
LIMITS = {
    "interval_ms": (100, 60000),
    "highlight_seconds": (0, 60),
    "highlight_alpha": (0.0, 1.0),
    "about_volume": (0.0, 1.0),
    "history_samples": (60, 100000),
    "exit_history_size": (0, 100000),
}
CHOICES = {
    "color_scheme": ("system", "light", "dark"),
    "highlight_mode": ("user", "category", "none"),
    "services_scope": ("both", "system", "user"),
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _same_kind(value, default):
    """Whether a stored value has the type of its default (bool, number, text, list or dict)."""
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _number(value, default, limits=None):
    """A stored number as the type of its default (600.0 -> 600), within `limits`; None if it is none."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        return None
    if limits is not None:
        value = min(max(value, limits[0]), limits[1])
    return round(value) if isinstance(default, int) else float(value)


def _color(value):
    return isinstance(value, str) and _COLOR.fullmatch(value) is not None


class Config(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, path=None):
        super().__init__()
        base = os.path.join(GLib.get_user_config_dir(), "procular")
        self.path = path or os.path.join(base, "settings.json")
        self.data = copy.deepcopy(DEFAULTS)
        self._save_source = 0
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:  # -sig: editors may add a BOM
                stored = json.load(fh)
        except OSError:
            return
        except (ValueError, RecursionError) as err:  # damaged, e.g. a typo made by hand
            self._set_aside(err)
            return
        if not isinstance(stored, dict):
            self._set_aside("not a JSON object")
            return
        for key, value in stored.items():
            if key not in DEFAULTS or not _same_kind(value, DEFAULTS[key]):
                continue  # unknown, or damaged by hand: a wrong type would crash the code using it
            default = DEFAULTS[key]
            if key == "highlights":
                for hkey, hval in value.items():
                    if hkey in self.data["highlights"] and isinstance(hval, dict):
                        entry = self.data["highlights"][hkey]
                        if _color(hval.get("color")):
                            entry["color"] = hval["color"]
                        if isinstance(hval.get("enabled"), bool):
                            entry["enabled"] = hval["enabled"]
            elif key == "user_colors":
                self.data[key] = {str(k): v for k, v in value.items() if _color(v)}
            elif key == "columns":
                self.data[key] = list(dict.fromkeys(c for c in value if isinstance(c, str)))
            elif key == "column_widths":
                widths = {k: _number(v, 0, (20, 10000)) for k, v in value.items()}
                self.data[key] = {k: v for k, v in widths.items() if v is not None}
            elif key == "window":
                for field in ("width", "height"):
                    size = _number(value.get(field), 0, (200, 100000))
                    if size is not None:
                        self.data[key][field] = size
                if isinstance(value.get("maximized"), bool):
                    self.data[key]["maximized"] = value["maximized"]
            elif key == "sort":
                self.data[key] = {
                    name: state for name, state in value.items()
                    if state is None or (isinstance(state, dict) and isinstance(state.get("column"), str))}
            elif key in CHOICES:
                if value in CHOICES[key]:
                    self.data[key] = value
            elif isinstance(default, (int, float)) and not isinstance(default, bool):
                number = _number(value, default, LIMITS.get(key))
                if number is not None:
                    self.data[key] = number
            else:
                self.data[key] = value

    def _set_aside(self, reason):
        """A file that cannot be read would be overwritten with the defaults on the next save:
        keep it for the user to repair instead."""
        backup = self.path + ".broken"
        try:
            os.replace(self.path, backup)
        except OSError:
            backup = None
        print(f"Procular: cannot read the settings in {self.path} ({reason}); using the defaults"
              + (f", the file was moved to {backup}" if backup else ""), file=sys.stderr)

    def save_now(self):
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
        try:
            path = os.path.realpath(self.path)  # a symlink (e.g. into a dotfiles repository) stays one
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            # a temporary file of its own: other instances (e.g. the headless test runs) may save too
            fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, indent=2)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError:
            pass
        return False

    def _schedule_save(self):
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(800, self._on_save_timeout)

    def _on_save_timeout(self):
        self._save_source = 0
        self.save_now()
        return False

    def get(self, key):
        return self.data[key]

    def set(self, key, value):
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self._schedule_save()
        self.emit("changed", key)

    def get_sort(self, name, default=None):
        """How the list `name` was sorted the last time: {"column": id, "descending": bool} or None."""
        sorts = self.data["sort"]
        return sorts[name] if name in sorts else default

    def set_sort(self, name, state):
        sorts = self.data["sort"]
        if name in sorts and sorts[name] == state:
            return
        sorts[name] = state
        self._schedule_save()
        self.emit("changed", "sort")

    def set_user_color(self, uid, color):
        """Pick the row colour of a user's processes; None goes back to the default."""
        colors = self.data["user_colors"]
        key = str(uid)
        if colors.get(key) == color:
            return
        if color is None:
            del colors[key]
        else:
            colors[key] = color
        self._schedule_save()
        self.emit("changed", "user_colors")

    def reset_user_colors(self):
        if self.data["user_colors"]:
            self.data["user_colors"] = {}
            self._schedule_save()
            self.emit("changed", "user_colors")

    def set_highlight(self, hkey, field, value):
        entry = self.data["highlights"][hkey]
        if entry.get(field) == value:
            return
        entry[field] = value
        self._schedule_save()
        self.emit("changed", "highlights")
