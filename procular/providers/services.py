"""systemd units over D-Bus (system and user manager)."""

import os
import threading
import time

from gi.repository import Gio, GLib

from ..util.linux import systemd_unescape

SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"


class ServicesProvider:
    """The units of the system manager ("system") and of the user's own manager ("user").

    The list has two sources. ListUnits (what is loaded, with its state) is answered from
    memory and is polled. ListUnitFiles (what is installed, and whether it is enabled) makes
    systemd read and evaluate every unit file: seconds of CPU time in PID 1 on a machine with
    some hundred of them, during which it serves nobody else. So the unit files are read
    once, in a request of their own (load_unit_files), and again only when they changed:
    systemd announces that (watch()), and where it does not they are read again after
    FILES_MAX_AGE.
    """

    FILES_MAX_AGE = 300.0  # seconds; only for a manager that does not send us its signals
    FILES_RETRY = 30.0  # seconds until unit files that could not be read are asked for again
    SIGNAL_SETTLE_MS = 1500  # one "systemctl enable" sends several signals: wait for the last

    def __init__(self):
        self._buses = {}
        self._lock = threading.Lock()  # for the fields below: workers read the unit files
        self._files = {}  # scope -> (generation, monotonic time, {unit file name: state})
        self._generation = {}  # scope -> number; raised whenever the unit files may have changed
        self._retry_at = {}  # scope -> monotonic time before which a failed read is not repeated
        self._watching = set()  # scopes whose signals are subscribed (main thread)
        self._watched = set()  # ... and whose manager confirmed that it sends them
        self._changed = set()  # scopes with a signal that is still settling
        self._settle = 0
        self._listeners = []

    def bus(self, scope):
        with self._lock:
            bus = self._buses.get(scope)
        if bus is not None:
            return bus
        try:
            bus_type = Gio.BusType.SYSTEM if scope == "system" else Gio.BusType.SESSION
            bus = Gio.bus_get_sync(bus_type, None)
        except GLib.Error:
            return None
        with self._lock:
            self._buses[scope] = bus
        return bus

    def _call_sync(self, scope, method, params, reply_type, timeout_ms=5000):
        bus = self.bus(scope)
        if bus is None:
            return None
        try:
            return bus.call_sync(SYSTEMD, SYSTEMD_PATH, MANAGER_IFACE, method, params,
                                 GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error:
            return None

    # ---- unit files

    def unit_files_outdated(self, scope):
        """Whether load_unit_files() has something to do for this scope. Any thread."""
        now = time.monotonic()
        with self._lock:
            entry = self._files.get(scope)
            if now < self._retry_at.get(scope, 0):
                return False
            if entry is None or entry[0] != self._generation.get(scope, 0):
                return True
            return scope not in self._watched and now - entry[1] > self.FILES_MAX_AGE

    def load_unit_files(self, scopes):
        """Read the unit files of the scopes that need it. Runs in a worker thread and can take
        seconds. Returns True if a list was read."""
        loaded = False
        for scope in scopes:
            if not self.unit_files_outdated(scope):
                continue
            with self._lock:
                generation = self._generation.get(scope, 0)
            reply = self._call_sync(scope, "ListUnitFiles", None, "(a(ss))", timeout_ms=60000)
            now = time.monotonic()
            with self._lock:
                if reply is None:
                    self._retry_at[scope] = now + self.FILES_RETRY
                    continue
                files = {os.path.basename(path): state for path, state in reply.unpack()[0]}
                # generation: the one from before the call, so a change during it is read again
                self._files[scope] = (generation, now, files)
                loaded = True
        return loaded

    def invalidate_unit_files(self, scope):
        """The unit files of this scope may have changed. Any thread."""
        with self._lock:
            self._generation[scope] = self._generation.get(scope, 0) + 1
            self._retry_at.pop(scope, None)

    def watch(self, scopes, listener=None):
        """Have the managers of these scopes tell us when their unit files change; listener()
        is then called on the main loop. Main thread; scopes already watched are left alone."""
        if listener is not None and listener not in self._listeners:
            self._listeners.append(listener)
        for scope in scopes:
            if scope in self._watching:
                continue
            bus = self.bus(scope)
            if bus is None:
                continue
            self._watching.add(scope)
            for name in ("UnitFilesChanged", "Reloading"):  # enable / disable, daemon-reload
                bus.signal_subscribe(SYSTEMD, MANAGER_IFACE, name, SYSTEMD_PATH, None,
                                     Gio.DBusSignalFlags.NONE, self._on_manager_signal, scope)
            # systemd only sends its signals to clients that asked for them
            bus.call(SYSTEMD, SYSTEMD_PATH, MANAGER_IFACE, "Subscribe", None, None,
                     Gio.DBusCallFlags.NONE, -1, None, self._on_subscribed, scope)

    def _on_subscribed(self, bus, res, scope):
        try:
            bus.call_finish(res)
        except GLib.Error as err:
            if "AlreadySubscribed" not in (err.message or ""):
                return  # no signals: the unit files are read again now and then instead
        with self._lock:
            self._watched.add(scope)

    def _on_manager_signal(self, _bus, _sender, _path, _iface, signal, params, scope):
        if signal == "Reloading" and params is not None:
            try:
                if params.unpack()[0]:
                    return  # reload just started; wait for Reloading(False) when systemd is done
            except (IndexError, TypeError):
                pass
        self._changed.add(scope)
        if self._settle:
            GLib.source_remove(self._settle)
        self._settle = GLib.timeout_add(self.SIGNAL_SETTLE_MS, self._on_settled)

    def _on_settled(self):
        self._settle = 0
        changed, self._changed = self._changed, set()
        for scope in changed:
            self.invalidate_unit_files(scope)
        for listener in list(self._listeners):
            listener()
        return GLib.SOURCE_REMOVE

    # ---- units

    def list_units(self, scopes):
        """Runs in a worker thread. Returns ({(scope, name): unit dict}, files): the loaded
        units plus the unit files read so far (see load_unit_files) that are not loaded.
        `files` identifies the unit file lists that went into it and changes when they do."""
        result = {}
        used = []
        for scope in scopes:
            reply = self._call_sync(scope, "ListUnits", None, "(a(ssssssouso))")
            if reply is None:
                continue
            with self._lock:
                entry = self._files.get(scope)
            files = entry[2] if entry is not None else {}
            used.append((scope, entry[1] if entry is not None else None))
            for (name, desc, load, active, sub, _following, path, job_id, job_type, _job_path) in reply.unpack()[0]:
                result[(scope, name)] = {
                    "scope": scope,
                    "name": name,
                    "label": systemd_unescape(name),
                    "description": desc,
                    "load": load,
                    "active": active,
                    "sub": sub,
                    "path": path,
                    "job": job_type if job_id else "",
                    "enabled": files.get(name, ""),
                    "type": name.rsplit(".", 1)[-1] if "." in name else "",
                }
            # unit files that are not loaded (disabled services) — like a services list showing stopped services
            for fname, state in files.items():
                key = (scope, fname)
                # Left out: templates ("getty@.service", "foo@.socket") cannot be started as such,
                # and an alias ("dbus.service" -> dbus-broker.service) is another name of a unit
                # listed under its own name: as a row of its own it would look stopped.
                if key in result or "@." in fname or state == "alias":
                    continue
                result[key] = {
                    "scope": scope, "name": fname, "label": systemd_unescape(fname), "description": "", "load": "not-loaded",
                    "active": "inactive", "sub": "dead", "path": "", "job": "", "enabled": state,
                    "type": fname.rsplit(".", 1)[-1] if "." in fname else "",
                }
        return result, tuple(used)

    # ---- actions (main thread, async; polkit prompts are triggered by systemd)

    def unit_action(self, scope, name, action, callback):
        bus = self.bus(scope)
        if bus is None:
            callback(False, "D-Bus not available")
            return
        if action in ("StartUnit", "StopUnit", "RestartUnit", "ReloadUnit", "TryRestartUnit"):
            params = GLib.Variant("(ss)", (name, "replace"))
        elif action == "EnableUnitFiles":
            params = GLib.Variant("(asbb)", ([name], False, False))
        elif action == "DisableUnitFiles":
            params = GLib.Variant("(asb)", ([name], False))
        else:
            callback(False, f"Unknown action {action}")
            return

        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as err:
                callback(False, _dbus_message(err))
                return
            if action in ("EnableUnitFiles", "DisableUnitFiles"):
                with self._lock:
                    watched = scope in self._watched
                if not watched:  # else systemd's signals say so, once it has finished
                    self.invalidate_unit_files(scope)
                conn.call(SYSTEMD, SYSTEMD_PATH, MANAGER_IFACE, "Reload", None, None,
                          Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, GLib.MAXINT, None, None)
            callback(True, "")

        # no timeout: systemd answers once polkit has asked the user, and the 25 seconds D-Bus
        # waits by default would report a failure while the password is still being typed
        bus.call(SYSTEMD, SYSTEMD_PATH, MANAGER_IFACE, action, params, None,
                 Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, GLib.MAXINT, None, done)

    def unit_properties(self, scope, unit_path, callback):
        """Fetch all properties of a loaded unit (Unit + Service interfaces)."""
        bus = self.bus(scope)
        if bus is None or not unit_path:
            callback({})
            return
        props = {}
        pending = ["org.freedesktop.systemd1.Unit", "org.freedesktop.systemd1.Service"]

        def fetch_next():
            if not pending:
                callback(props)
                return
            iface = pending.pop(0)
            bus.call(SYSTEMD, unit_path, "org.freedesktop.DBus.Properties", "GetAll",
                     GLib.Variant("(s)", (iface,)), GLib.VariantType.new("(a{sv})"),
                     Gio.DBusCallFlags.NONE, 5000, None, on_reply)

        def on_reply(conn, res):
            try:
                props.update(conn.call_finish(res).unpack()[0])
            except GLib.Error:
                pass
            fetch_next()

        fetch_next()


def _dbus_message(err):
    msg = err.message
    if "GDBus.Error:" in msg:
        msg = msg.split(":", 2)[-1].strip()
    return msg
