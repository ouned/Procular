"""Map processes to application icons using desktop files and cgroup app ids."""

import os
import re

from gi.repository import Gio

_WRAPPERS = {"env", "sh", "bash", "flatpak", "snap", "python", "python3", "gjs", "java", "electron", "mono"}
# parts of application ids and paths that say nothing about which program it is
_NOISE = {"com", "org", "net", "app", "apps", "desktop", "github", "gitlab", "gnome", "kde", "freedesktop",
          "usr", "bin", "sbin", "lib", "lib64", "libexec", "opt", "share", "snap", "local", "home", "current"}


def _words(text):
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _NOISE}


def app_keys(app_id, exe="", wm_class="", commandline=""):
    """Lower-case names an application's own processes go by.

    From the application id (only its last part when it is a reverse-DNS name: the vendor says
    nothing) and from its desktop file: the program it starts, also inside "flatpak run
    --command=...", and the window class."""
    app_id = app_id.lower()
    keys = {app_id} | _words(app_id.rsplit(".", 1)[-1])
    names = [wm_class, os.path.basename(exe)]
    m = re.search(r"--command=(\S+)", commandline or "")
    if m:
        names.append(os.path.basename(m.group(1)))
    for name in names:
        name = name.lower()
        if name and name not in _WRAPPERS:
            keys.add(name)
            keys |= _words(name)
    return keys


# where applications are installed: a directory named after one says whose file it is. Not so
# below a home directory, where "~/code/tool" says nothing about the editor it was started from.
_INSTALL_DIRS = ("/usr/", "/opt/", "/app/", "/snap/", "/var/lib/")
# interpreter options that take a value, which therefore is not the script: bash --init-file FILE
_VALUE_OPTIONS = {"--init-file", "--rcfile", "-O", "+O", "-o", "+o", "-W", "-X", "-I", "-C", "-cp", "-classpath",
                  "--class-path", "-u", "--unset", "-L", "--module-path", "-p"}


def _script(cmdline):
    """What an interpreter was told to run ("python3 /usr/bin/lollypop", "gjs -m FILE", "java -jar
    FILE"), or "" for an interactive one and for inline code (-c)."""
    args = cmdline.split()[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-m", "-jar"):
            return args[i + 1] if i + 1 < len(args) else ""
        if arg in ("-c", "-e", "--command"):
            return ""
        if arg in _VALUE_OPTIONS:
            i += 2
        elif arg.startswith(("-", "+")) or "=" in arg.split("/")[0]:  # an option, or env's VAR=value
            i += 1
        else:
            return arg
    return ""


def _is_app_file(path, keys):
    """Whether a program or script file is named after the application or installed under its name."""
    base = os.path.basename(path)
    # "chrome" for google-chrome, "gimp-3.0" for gimp, "org.gnome.Weath" (cut off by the kernel)
    if base and any(key in base or (len(base) >= 3 and base in key) for key in keys):
        return True
    # /usr/lib/libreoffice/program/soffice.bin
    return path.startswith(_INSTALL_DIRS) and bool(_words(os.path.dirname(path)) & keys)


def is_app_process(rec, keys):
    """Whether a process in an application's cgroup is the application itself.

    Everything an application starts lands in its cgroup: the shells of a terminal, the tools
    an editor runs. Those are not the application and must not carry its icon."""
    name = (rec.get("name") or "").lower()
    exe = (rec.get("exe") or "").lower()
    base = os.path.basename(exe)
    if name in _WRAPPERS or base.rstrip("0123456789.") in _WRAPPERS:
        # an interpreter is what it runs: the application when that is the application's script
        # ("python3 /usr/bin/lollypop"), but not a shell the application opened for the user,
        # whatever files of the application its options name (bash --init-file /usr/share/code/...)
        script = _script((rec.get("cmdline") or "").lower())
        return bool(script) and _is_app_file(script, keys)
    if _is_app_file(name, keys) or _is_app_file(exe, keys):
        return True
    # the application's own files in a Flatpak; the runtime lives in /usr
    return rec.get("sandboxed") == "flatpak" and exe.startswith("/app/")


GENERIC_ICON = Gio.ThemedIcon.new("application-x-executable-symbolic")
KERNEL_ICON = Gio.ThemedIcon.new("emblem-system-symbolic")
SERVICE_ICON = Gio.ThemedIcon.new("system-run-symbolic")


def _showable(icon):
    """False for an image file GTK cannot draw (python3.desktop names an .xpm): the cell would be empty."""
    if not isinstance(icon, Gio.FileIcon):
        return True
    path = icon.get_file().get_path() or ""
    return os.path.exists(path) and not path.lower().endswith(".xpm")


class IconResolver:
    def __init__(self):
        self._index = None
        self._cache = {}
        self._apps = {}  # app id -> (icon, app_keys())

    def _build_index(self):
        index = {}
        for info in Gio.AppInfo.get_all():
            if not isinstance(info, Gio.DesktopAppInfo):
                continue
            icon = info.get_icon()
            if icon is None or not _showable(icon):
                continue
            keys = set()
            app_id = (info.get_id() or "").removesuffix(".desktop")
            if app_id:
                keys.add(app_id.lower())
                keys.add(app_id.split(".")[-1].lower())
            wm_class = info.get_startup_wm_class()
            if wm_class:
                keys.add(wm_class.lower())
            exe = info.get_executable()
            if exe:
                base = os.path.basename(exe).lower()
                if base not in _WRAPPERS:
                    keys.add(base)
            for key in keys:
                index.setdefault(key, icon)
        self._index = index

    def lookup_app(self, app_id):
        if not app_id:
            return None
        try:
            info = Gio.DesktopAppInfo.new(app_id + ".desktop")
        except TypeError:
            info = None
        if info is not None:
            icon = info.get_icon()
            if icon is not None and _showable(icon):
                return icon
        if self._index is None:
            self._build_index()
        return self._index.get(app_id.lower())

    def _app(self, app_id):
        """(icon, app_keys()) of an application id, or (None, None) when it has no icon."""
        found = self._apps.get(app_id)
        if found is None:
            try:
                info = Gio.DesktopAppInfo.new(app_id + ".desktop")
            except TypeError:
                info = None
            icon = self.lookup_app(app_id)
            if icon is None:
                found = (None, None)
            elif info is None:
                found = (icon, app_keys(app_id))
            else:
                found = (icon, app_keys(app_id, info.get_executable() or "", info.get_startup_wm_class() or "",
                                        info.get_commandline() or ""))
            self._apps[app_id] = found
        return found

    def resolve(self, rec):
        app_icon = None
        if rec.get("app_id") and not rec.get("kthread"):
            app_icon, keys = self._app(rec["app_id"])
            if app_icon is not None and not is_app_process(rec, keys):
                app_icon = None
        is_sys_svc = rec.get("unit_kind") == "service" and rec.get("unit_scope") == "system"
        key = (rec.get("app_id") if app_icon is not None else None, rec.get("name"),
               rec.get("exe"), rec.get("kthread"), is_sys_svc)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        icon = None
        if rec.get("kthread"):
            icon = KERNEL_ICON
        if icon is None:
            icon = app_icon
        if icon is None:
            if self._index is None:
                self._build_index()
            name = (rec.get("name") or "").lower()
            icon = self._index.get(name)
            if icon is None and rec.get("exe"):
                icon = self._index.get(os.path.basename(rec["exe"]).lower())
        if icon is None:
            icon = SERVICE_ICON if rec.get("unit_kind") == "service" and rec.get("unit_scope") == "system" \
                else GENERIC_ICON
        self._cache[key] = icon
        return icon
