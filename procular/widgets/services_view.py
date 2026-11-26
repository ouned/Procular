"""Services tab: systemd system and user units."""

import time

from gi.repository import Adw, Gio, GLib, Gtk

from ..models import highlight
from ..providers.system import read_cgroup_pressure
from ..util import linux
from ..util.format import fmt_bytes, fmt_timestamp
from ..util.lifecycle import destroy_on_close, is_destroyed
from ..util.tasks import run_async
from .table import Column, Table

RUNNING_SUBS = ("running", "listening", "waiting")
SERVICE_FILTER_FIELDS = ("label", "name", "description", "active", "sub", "scope", "user", "enabled", "load", "pid")


def service_status(data):
    """State name for the row's status dot (css class status-<state>)."""
    active = data.get("active")
    if active == "failed":
        return "failed"
    if active == "active":
        return "running" if data.get("sub") in RUNNING_SUBS else "active"
    if active in ("activating", "deactivating", "reloading", "refreshing", "maintenance"):
        return "changing"
    return "inactive"


# PSI "some" over the last 10 seconds: from a stall now and then to a unit that mostly waits
_PSI_STEPS = (1.0, 5.0, 10.0, 25.0, 50.0)
_PSI_LABELS = {"cpu": "CPU", "memory": "Memory", "io": "I/O"}
_PSI_SHORT = {"cpu": "CPU", "memory": "Mem", "io": "I/O"}


def _fmt_psi(value):
    return "" if not value else f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


def _heat_psi(value):
    level = 0
    for step in _PSI_STEPS:
        if value < step:
            break
        level += 1
    return level


def psi_tooltip(pressure, resource):
    """"Memory: some 2.1% · 1.0% · 0.5%, all 0.3% · 0.1% · 0.0%": the kernel's averages over
    10 s, 1 min and 5 min of one resource's pressure; None when unknown."""
    entry = (pressure or {}).get(resource)
    if not entry:
        return None
    parts = []
    for kind, label in (("some", "some"), ("full", "all")):
        values = entry.get(kind)
        if values:
            parts.append(f"{label} {values['avg10']:.1f}% · {values['avg60']:.1f}% · {values['avg300']:.1f}%")
    return f"{_PSI_LABELS[resource]}: {', '.join(parts)}" if parts else None


def worst_pressure(pressure):
    """(percent, resource) of the resource whose tasks waited the most in the last 10 seconds
    ("some" share), or None when nothing waited or nothing is known."""
    worst = None
    for resource in ("memory", "io", "cpu"):
        value = ((pressure or {}).get(resource) or {}).get("some", {}).get("avg10")
        if value and (worst is None or value > worst[0]):
            worst = (value, resource)
    return worst


def psi_column():
    """The pressure of a row carrying "pressure" (read_cgroup_pressure()): the resource that
    waited the most, all three in the tooltip."""
    def tooltip(d):
        lines = [psi_tooltip(d.get("pressure"), resource) for resource in ("cpu", "memory", "io")]
        lines = [line for line in lines if line]
        return "\n".join(["Time tasks waited, over 10 s · 1 min · 5 min", *lines]) if lines else None

    return Column("pressure", "Pressure", lambda d: worst_pressure(d.get("pressure")),
                  fmt=lambda w: f"{_PSI_SHORT[w[1]]} {_fmt_psi(w[0])}" if w else "", numeric=True, width=95,
                  heat=lambda w: _heat_psi(w[0]), sort=lambda d: (worst_pressure(d.get("pressure")) or (0.0,))[0],
                  tooltip=tooltip)


def load_units(provider, scopes, cgroups):
    """The units (see ServicesProvider.list_units) and the pressure of those with processes:
    {(scope, unit): read_cgroup_pressure()}. Runs in a worker."""
    units, files = provider.list_units(scopes)
    pressure = {}
    for key, cgroup in cgroups.items():
        reading = read_cgroup_pressure(cgroup)
        if reading:
            pressure[key] = reading
    return units, files, pressure


def service_hl(data):
    active = data.get("active")
    if active == "failed":
        return "svc-failed"
    if active == "active" and data.get("sub") in RUNNING_SUBS:
        return "svc-running"
    if active in ("inactive", "deactivating", None):
        return "svc-inactive"
    return ""


class ServicesView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.config = win.config
        self.provider = win.app.services
        self._active = False
        self._loading = False
        self._refresh_again = False
        self._loading_files = False
        self._files_shown = None  # which unit file lists the rows on screen were made from
        self._timer = 0

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.scope = Gtk.DropDown.new_from_strings(["System and user", "System", "User"])
        self.scope.set_selected({"both": 0, "system": 1, "user": 2}.get(self.config.get("services_scope"), 0))
        self.scope.connect("notify::selected", self._on_scope_changed)
        self.only_services = Gtk.CheckButton(label="Services only", active=self.config.get("services_only"))
        self.only_services.connect("toggled", self._on_only_toggled)
        bar.append(self.scope)
        bar.append(self.only_services)
        bar.append(Gtk.Box(hexpand=True))
        controls = Gtk.Box(css_classes=["linked"])
        for label, icon, action in (
            ("Start", "media-playback-start-symbolic", "StartUnit"),
            ("Stop", "media-playback-stop-symbolic", "StopUnit"),
            ("Restart", "view-refresh-symbolic", "RestartUnit"),
        ):
            btn = Gtk.Button(icon_name=icon, tooltip_text=label)
            btn.connect("clicked", lambda _b, a=action: self._unit_action(a))
            controls.append(btn)
        bar.append(controls)
        journal = Gtk.Button(label="Journal")
        journal.connect("clicked", lambda *_: self._show_journal())
        props = Gtk.Button(label="Properties")
        props.connect("clicked", lambda *_: self._show_properties())
        bar.append(journal)
        bar.append(props)
        self.count = Gtk.Label(css_classes=["dim"], margin_start=6)
        bar.append(self.count)
        self.append(bar)

        columns = [
            # the status dot leads the row: a type icon would be the same gear on nearly every one
            Column("name", "Name", "label", width=340, indicator=service_status,
                   tooltip=lambda d: d["name"] if d["name"] != d["label"] else None),
            Column("description", "Description", "description", width=260, expand=True),
            Column("scope", "Scope", "scope", width=70),
            Column("user", "User", "user", width=100),
            Column("active", "Status", "active", width=90),
            Column("sub", "State", "sub", width=90),
            Column("enabled", "Startup", "enabled", width=90),
            Column("pid", "PID", "pid", numeric=True, width=70),
            Column("procs", "Processes", "procs", numeric=True, width=80),
            Column("rss", "Memory", "rss", fmt=fmt_bytes, numeric=True, width=90),
            Column("cpu", "CPU", "cpu", fmt=lambda v: f"{v:.2f}" if v else "", numeric=True, width=60),
            psi_column(),
            Column("load", "Load", "load", width=90, visible=False),
            Column("job", "Job", "job", width=70, visible=False),
        ]
        self.table = Table(columns, key=lambda d: (d["scope"], d["name"]),
                           filter_keys=SERVICE_FILTER_FIELDS, menu_model=self._build_menu())
        self.table.hl_func = self._row_hl
        self.table.connect("activated", lambda _t, _d: self._show_properties())
        self.append(self.table)
        self.table.remember_sort(self.config, "services", "name")
        self._install_actions()
        self._color_by_user = self.config.get("highlight_mode") == "user"
        self.config.connect("changed", self._on_config_changed)

    def _row_hl(self, data):
        """Status style of a unit plus, when rows are coloured by user, its processes' user."""
        hl = service_hl(data)
        uid = data.get("uid")
        if uid is None or not self._color_by_user:
            return hl
        return f"{hl} {highlight.user_class(uid)}" if hl else highlight.user_class(uid)

    def _on_config_changed(self, _config, key):
        if key == "highlight_mode":
            self._color_by_user = self.config.get("highlight_mode") == "user"
            if self._active:
                self.refresh()

    # ------------------------------------------------------------------
    def _build_menu(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        for label, action in (("Start", "StartUnit"), ("Stop", "StopUnit"), ("Restart", "RestartUnit"),
                              ("Reload", "ReloadUnit")):
            s1.append(label, f"svc.action('{action}')")
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append("Enable at Startup", "svc.action('EnableUnitFiles')")
        s2.append("Disable at Startup", "svc.action('DisableUnitFiles')")
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Go to Process", "svc.goto")
        s3.append("Show Journal", "svc.journal")
        s3.append("Copy", "svc.copy")
        s3.append("Properties", "svc.properties")
        menu.append_section(None, s3)
        return menu

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        act = Gio.SimpleAction.new("action", GLib.VariantType.new("s"))
        act.connect("activate", lambda _a, v: self._unit_action(v.get_string()))
        group.add_action(act)
        for name, handler in (("goto", self._goto), ("journal", self._show_journal),
                              ("copy", self.table.copy_selected), ("properties", self._show_properties)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, h=handler: h())
            group.add_action(a)
        self.insert_action_group("svc", group)

    # ------------------------------------------------------------------ polling
    def set_active(self, active):
        if active == self._active:
            return
        self._active = active
        if active:
            self.provider.watch(self._scopes(), self._on_unit_files_changed)
            self.refresh(force=True)
            if not self._timer:
                self._timer = GLib.timeout_add_seconds(2, self._on_timer)
        elif self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_timer(self):
        if self._active and not self.win.sampler.paused:
            self.refresh()
        return True

    def _scopes(self):
        return {0: ("system", "user"), 1: ("system",), 2: ("user",)}[self.scope.get_selected()]

    def refresh(self, force=False):
        if self._loading:
            if force:
                self._refresh_again = True
            return
        self._loading = True
        scopes = self._scopes()
        run_async(load_units, lambda result, error: self._on_units(result, error, scopes),
                  self.provider, scopes, self._unit_cgroups())

    def _unit_processes(self):
        """(scope, unit id, ProcessItem) of every running process that belongs to a unit we list."""
        my_uid = self.win.my_uid
        for item in self.win.tree.items.values():
            rec = item.rec
            unit = rec.get("unit_id")
            if not unit or item.terminated:
                continue
            scope = rec.get("unit_scope") or "system"
            if scope == "user" and rec.get("unit_uid") != my_uid:
                continue  # a unit of another user's manager: not in our lists, whatever its name
            yield scope, unit, item

    def _unit_cgroups(self):
        """{(scope, unit): cgroup path} of the units with processes, for their pressure files."""
        cgroups = {}
        for scope, unit, item in self._unit_processes():
            if (scope, unit) not in cgroups:
                path = linux.unit_cgroup(item.rec.get("cgroup"), unit)
                if path:
                    cgroups[(scope, unit)] = path
        return cgroups

    def _load_unit_files(self):
        """Read the unit files if they are not known or have changed.

        On their own, not with every list: reading them keeps systemd busy for seconds (see
        ServicesProvider). And only after a list has arrived: systemd answers one request at
        a time, so the loaded units would wait for it too."""
        scopes = self._scopes()
        if self._active and not self._loading_files and any(self.provider.unit_files_outdated(s) for s in scopes):
            self._loading_files = True
            run_async(self.provider.load_unit_files, self._on_unit_files, scopes)

    def _on_unit_files(self, loaded, _error):
        self._loading_files = False
        if self._active and (loaded or any(self.provider.unit_files_outdated(s) for s in self._scopes())):
            self.refresh(force=True)  # show what was read; or read again what changed meanwhile

    def _on_unit_files_changed(self):
        """systemd reported new, removed, enabled or disabled unit files."""
        if self._active:
            self.refresh(force=True)

    def _on_units(self, result, error, asked_scopes):
        self._loading = False
        again, self._refresh_again = self._refresh_again, False
        if not self._active:
            return
        if again or asked_scopes != self._scopes():
            self.refresh()
            return
        if error or result is None:
            return
        units, files, pressure = result
        # aggregate processes per unit from the process tree
        per_unit = {}
        for scope, unit, item in self._unit_processes():
            rec = item.rec
            key = (scope, unit)
            agg = per_unit.get(key)
            if agg is None:
                per_unit[key] = agg = {"pid": rec["pid"], "procs": 0, "rss": 0, "cpu": 0.0,
                                       "uid": rec.get("uid"), "user": rec.get("user")}
            agg["procs"] += 1
            agg["rss"] += rec["rss"]
            agg["cpu"] += rec["cpu"]
            if rec["pid"] < agg["pid"]:
                # the unit's user is that of the process shown in the PID column
                agg["pid"], agg["uid"], agg["user"] = rec["pid"], rec.get("uid"), rec.get("user")
        only = self.only_services.get_active()
        rows = []
        for key, unit in units.items():
            if only and unit["type"] != "service":
                continue
            row = dict(unit)
            agg = per_unit.get(key)
            row["pid"] = agg["pid"] if agg else None
            row["procs"] = agg["procs"] if agg else None
            row["rss"] = agg["rss"] if agg else None
            row["cpu"] = agg["cpu"] if agg else None
            row["uid"] = agg["uid"] if agg else None
            row["user"] = agg["user"] if agg else None
            row["pressure"] = pressure.get(key) if agg else None
            rows.append(row)
        # A manager that did not answer this time (busy with ListUnitFiles or a reload, or timed
        # out) still has its units: keep showing them rather than let them all vanish for a cycle.
        answered = {scope for scope, _read in files}
        missing = set(asked_scopes) - answered
        if missing:
            rows += [r for r in self.table.all_rows(removed=False) if r["scope"] in missing]
        # units that only show up because their unit file was read now are not new units
        flash = self.config.get("highlight_seconds") if files == self._files_shown else None
        if not missing:
            self._files_shown = files
        self.table.set_rows(rows, highlight=flash or None, now=time.monotonic())
        self._load_unit_files()
        running = sum(1 for r in rows if r["active"] == "active")
        text = f"{len(rows)} units, {running} active"
        if self._loading_files and any(read is None for _scope, read in files):
            text += " · reading unit files…"  # the units that are not loaded are still to come
        self.count.set_text(text)

    def _on_scope_changed(self, *_):
        self.config.set("services_scope", {0: "both", 1: "system", 2: "user"}[self.scope.get_selected()])
        self.table.clear()
        if self._active:
            self.provider.watch(self._scopes())
        self.refresh(force=True)

    def _on_only_toggled(self, btn):
        self.config.set("services_only", btn.get_active())
        self.table.clear()
        self.refresh(force=True)

    def set_filter_text(self, text):
        self.table.set_filter_text(text)

    # ------------------------------------------------------------------ actions
    def _selected(self):
        data = self.table.selected()
        if data is None:
            self.win.toast("Select a unit first")
        return data

    def _unit_action(self, action):
        data = self._selected()
        if data is None:
            return
        verbs = {"StartUnit": "Started", "StopUnit": "Stopped", "RestartUnit": "Restarted",
                 "ReloadUnit": "Reloaded", "EnableUnitFiles": "Enabled", "DisableUnitFiles": "Disabled"}

        def done(ok, err):
            self.win.toast(f"{verbs.get(action, action)} {data['label']}" if ok else f"Failed: {err}")
            GLib.timeout_add(500, lambda: self.refresh() and False)

        self.provider.unit_action(data["scope"], data["name"], action, done)

    def _goto(self):
        data = self._selected()
        if data is None:
            return
        if data.get("pid"):
            self.win.goto_process(data["pid"])
        else:
            self.win.toast("This unit has no running processes")

    def _show_journal(self):
        data = self._selected()
        if data is None:
            return
        JournalDialog(data).present(self.win)

    def _show_properties(self):
        data = self._selected()
        if data is None:
            return
        dialog = UnitPropertiesDialog(self.win, data)
        dialog.present(self.win)
        self.provider.unit_properties(data["scope"], data["path"], dialog.set_properties)


class JournalDialog(Adw.Dialog):
    def __init__(self, data):
        super().__init__(title=f"Journal — {data['label']}")
        destroy_on_close(self)
        self.set_content_width(1000)
        self.set_content_height(640)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        self.buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buffer, editable=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, left_margin=6, right_margin=6)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(view)
        toolbar.set_content(self.scroller)
        self.set_child(toolbar)
        argv = ["journalctl", "--no-pager", "-o", "short-iso", "-n", "500"]
        if data["scope"] == "user":
            argv += ["--user"]
        argv += ["-u", data["name"]]
        self.buffer.set_text("Loading…")
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
            proc.communicate_utf8_async(None, None, self._done)
        except GLib.Error as err:
            self.buffer.set_text(err.message)

    def _done(self, proc, res):
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error as err:
            out = err.message
        if is_destroyed(self):
            return  # closed before journalctl finished
        self.buffer.set_text(out or "No journal entries (you may need to be in the 'systemd-journal' or "
                                    "'adm' group to read system logs).")
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        if is_destroyed(self):
            return False
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False


def _fmt_usec_ts(v):
    return fmt_timestamp(v / 1e6) if v else "—"


def _fmt_exec(v):
    try:
        return "\n".join(" ".join(argv) for _path, argv, *_rest in v)
    except (TypeError, ValueError):
        return str(v)


UNIT_PROPS = [
    ("Id", "Name", str),
    ("Description", "Description", str),
    ("LoadState", "Load state", str),
    ("ActiveState", "Active state", str),
    ("SubState", "Sub state", str),
    ("UnitFileState", "Startup", str),
    ("FragmentPath", "Unit file", str),
    ("Type", "Service type", str),
    ("MainPID", "Main PID", lambda v: str(v) if v else "—"),
    ("ExecStart", "Command", _fmt_exec),
    ("User", "User", lambda v: v or "root / default"),
    ("Restart", "Restart policy", str),
    ("ActiveEnterTimestamp", "Active since", _fmt_usec_ts),
    ("MemoryCurrent", "Memory", lambda v: fmt_bytes(v) if v and v < 2**63 else "—"),
    ("CPUUsageNSec", "CPU time", lambda v: f"{v / 1e9:.2f} s" if v and v < 2**63 else "—"),
    ("TasksCurrent", "Tasks", lambda v: str(v) if v is not None and v < 2**63 else "—"),
    ("ControlGroup", "Control group", str),
    ("Documentation", "Documentation", lambda v: "\n".join(v) if v else "—"),
]


class UnitPropertiesDialog(Adw.Dialog):
    def __init__(self, win, data):
        super().__init__(title=data["label"])
        destroy_on_close(self)
        self.win = win
        self.data = data
        self.set_content_width(640)
        self.set_content_height(640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Loading…")
        self.page.add(self.group)
        toolbar.set_content(self.page)
        self.set_child(toolbar)

    def set_properties(self, props):
        if is_destroyed(self):
            return  # closed before systemd answered
        self.page.remove(self.group)
        self.group = Adw.PreferencesGroup(title=self.data["description"] or self.data["label"])
        if not props:
            self.group.set_description("This unit is not loaded; no runtime properties available.")
            props = {"Id": self.data["name"], "UnitFileState": self.data["enabled"]}
        for key, title, fmt in UNIT_PROPS:
            if key not in props:
                continue
            row = Adw.ActionRow(title=title, subtitle_selectable=True)
            row.set_subtitle(GLib.markup_escape_text(fmt(props[key]) or "—"))
            row.add_css_class("property")
            if key == "MainPID" and props[key]:
                btn = Gtk.Button(label="Go to process", valign=Gtk.Align.CENTER)
                btn.add_css_class("flat")
                pid = props[key]
                btn.connect("clicked", lambda *_: (self.close(), self.win.goto_process(pid)))
                row.add_suffix(btn)
            self.group.add(row)
        self.page.add(self.group)
