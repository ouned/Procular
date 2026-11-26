"""The services list: loaded units are polled, unit files are read once and when they changed."""

from types import SimpleNamespace

from procular.providers.services import ServicesProvider

UNITS = [("cups.service", "CUPS", "loaded", "active", "running", "", "/org/freedesktop/systemd1/unit/cups", 0, "", "/"),
         ("getty@tty1.service", "Getty", "loaded", "active", "running", "", "/org/x", 0, "", "/")]
FILES = [("/usr/lib/systemd/system/cups.service", "enabled"), ("/usr/lib/systemd/system/sshd.service", "disabled"),
         ("/usr/lib/systemd/system/getty@.service", "enabled")]


class _Reply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return (self.value,)


def _provider(monkeypatch, files=FILES):
    provider = ServicesProvider()
    calls = []

    def call(scope, method, params, reply_type, timeout_ms=5000):
        calls.append((scope, method))
        if method == "ListUnits":
            return _Reply(UNITS)
        return _Reply(files) if files is not None else None

    monkeypatch.setattr(provider, "_call_sync", call)
    return provider, calls


def test_listing_units_does_not_read_the_unit_files(monkeypatch):
    provider, calls = _provider(monkeypatch)
    for _ in range(3):
        units, _files = provider.list_units(["system"])
    assert calls == [("system", "ListUnits")] * 3
    assert set(units) == {("system", "cups.service"), ("system", "getty@tty1.service")}
    assert units[("system", "cups.service")]["enabled"] == ""  # not known yet


def test_unit_files_are_read_once_and_merged(monkeypatch):
    provider, calls = _provider(monkeypatch)
    assert provider.unit_files_outdated("system")
    _units, before = provider.list_units(["system"])
    assert provider.load_unit_files(["system"]) is True
    assert provider.load_unit_files(["system"]) is False  # nothing to do
    assert not provider.unit_files_outdated("system")
    units, after = provider.list_units(["system"])
    assert [c for c in calls if c[1] == "ListUnitFiles"] == [("system", "ListUnitFiles")]
    assert before != after  # the view tells from this that rows appear for another reason than new units
    assert units[("system", "cups.service")]["enabled"] == "enabled"
    # an installed unit that is not loaded is listed too; a template is not a unit
    assert units[("system", "sshd.service")]["load"] == "not-loaded"
    assert units[("system", "sshd.service")]["enabled"] == "disabled"
    assert ("system", "getty@.service") not in units
    assert provider.list_units(["system"])[1] == after


def test_unit_files_are_read_again_after_a_change(monkeypatch):
    provider, calls = _provider(monkeypatch)
    provider.load_unit_files(["system", "user"])
    provider.invalidate_unit_files("user")
    assert not provider.unit_files_outdated("system") and provider.unit_files_outdated("user")
    provider.load_unit_files(["system", "user"])
    assert [c[0] for c in calls if c[1] == "ListUnitFiles"] == ["system", "user", "user"]


def test_unit_files_changed_while_being_read_are_read_again(monkeypatch):
    provider, _calls = _provider(monkeypatch)
    real = provider._call_sync

    def slow(scope, method, *args, **kwargs):
        if method == "ListUnitFiles":
            provider.invalidate_unit_files(scope)  # systemd reports a change during the call
        return real(scope, method, *args, **kwargs)

    monkeypatch.setattr(provider, "_call_sync", slow)
    assert provider.load_unit_files(["system"]) is True
    assert provider.unit_files_outdated("system")


def test_unit_files_age_only_without_change_signals(monkeypatch):
    provider, _calls = _provider(monkeypatch)
    provider.load_unit_files(["system"])
    generation, _read, files = provider._files["system"]
    provider._files["system"] = (generation, -1e9, files)  # read long ago
    assert provider.unit_files_outdated("system")
    provider._watched.add("system")  # the manager tells us about changes
    assert not provider.unit_files_outdated("system")


def test_failed_read_of_unit_files_is_not_repeated_at_once(monkeypatch):
    provider, calls = _provider(monkeypatch, files=None)
    assert provider.load_unit_files(["system"]) is False
    assert not provider.unit_files_outdated("system")  # later: FILES_RETRY
    provider.list_units(["system"])
    assert [c for c in calls if c[1] == "ListUnitFiles"] == [("system", "ListUnitFiles")]
    provider._retry_at["system"] = 0
    assert provider.unit_files_outdated("system")


def test_reloading_signal_waits_for_completion(monkeypatch):
    from gi.repository import GLib

    provider = ServicesProvider()
    scheduled = []
    monkeypatch.setattr(GLib, "timeout_add", lambda ms, fn: scheduled.append(ms) or 1)
    monkeypatch.setattr(GLib, "source_remove", lambda sid: None)
    # Reloading(True) means systemd just started reloading: ignore it
    provider._on_manager_signal(None, None, None, None, "Reloading", GLib.Variant("(b)", (True,)), "system")
    assert scheduled == [] and not provider._changed
    # Reloading(False) means reload finished: schedule invalidation
    provider._on_manager_signal(None, None, None, None, "Reloading", GLib.Variant("(b)", (False,)), "system")
    assert len(scheduled) == 1 and provider._changed == {"system"}


def test_bus_retries_after_transient_error(monkeypatch):
    from gi.repository import Gio, GLib

    provider = ServicesProvider()
    attempts = []

    def fake_get(bus_type, cancellable):
        attempts.append(bus_type)
        if len(attempts) == 1:
            raise GLib.Error("temporary")
        return "fake-bus"

    monkeypatch.setattr(Gio, "bus_get_sync", fake_get)
    assert provider.bus("system") is None
    assert provider.bus("system") == "fake-bus"
    assert provider.bus("system") == "fake-bus"
    assert len(attempts) == 2


def test_aliases_and_templates_of_any_type_are_no_rows_of_their_own(monkeypatch):
    files = FILES + [("/usr/lib/systemd/system/dbus.service", "alias"),
                     ("/usr/lib/systemd/system/systemd-journald@.socket", "static"),
                     ("/usr/lib/systemd/system/blockdev@.target", "static")]
    provider, _calls = _provider(monkeypatch, files)
    provider.load_unit_files(["system"])
    units, _files = provider.list_units(["system"])
    assert {name for _scope, name in units} == {"cups.service", "getty@tty1.service", "sshd.service"}


def _psi(avg10):
    return {"some": {"avg10": avg10, "avg60": 0.0, "avg300": 0.0, "total": 1},
            "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0}}


def test_pressure_column_shows_the_resource_that_waited_most():
    from procular.widgets import services_view as sv

    col = sv.psi_column()
    busy = {"pressure": {"cpu": _psi(0.4), "memory": _psi(12.0), "io": _psi(3.0)}}
    assert col.fmt(col.get(busy)) == "Mem 12%"
    assert col.heat(col.get(busy)) == 3
    assert col.sort(busy) == 12.0
    tip = col.tooltip(busy).splitlines()
    assert tip[0].startswith("Time tasks waited") and tip[2] == "Memory: some 12.0% · 0.0% · 0.0%, all 0.0% · 0.0% · 0.0%"
    idle = {"pressure": {"cpu": _psi(0.0)}}
    assert col.get(idle) is None and col.fmt(col.get(idle)) == "" and col.sort(idle) == 0.0
    assert col.get({"pressure": None}) is None and col.tooltip({"pressure": None}) is None
    assert sv._fmt_psi(2.345) == "2.3%"


def test_load_units_reads_the_pressure_of_units_with_processes(monkeypatch):
    from procular.widgets import services_view as sv

    read = []

    def fake_read(cgroup):
        read.append(cgroup)
        return {"cpu": _psi(1.0)} if cgroup == "/system.slice/a.service" else {}

    monkeypatch.setattr(sv, "read_cgroup_pressure", fake_read)
    provider = SimpleNamespace(list_units=lambda scopes: ({("system", "a.service"): {}}, (("system", 1.0),)))
    units, files, pressure = sv.load_units(provider, ("system",), {("system", "a.service"): "/system.slice/a.service",
                                                                  ("system", "b.service"): "/system.slice/b.service"})
    assert files == (("system", 1.0),) and list(units) == [("system", "a.service")]
    assert pressure == {("system", "a.service"): {"cpu": _psi(1.0)}}  # b has no pressure files
    assert sorted(read) == ["/system.slice/a.service", "/system.slice/b.service"]
