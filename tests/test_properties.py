"""Tests for Properties helpers (elevated reads, socket state classes) and About teardown."""

from types import SimpleNamespace

import pytest
from gi.repository import GLib

from procular.helper import procular_helper
from procular.util import linux, privileged
from procular.windows.about import AboutDialog
from procular.widgets.network_view import _STATE_CLASS as _SOCKET_CLASS
from procular.windows.properties import LazyPage, ProcReader, ThreadsPage


def test_socket_state_classes_match_proc_net_states():
    for state in linux.TCP_STATES.values():
        if state == "Closed":
            continue
        assert state in _SOCKET_CLASS, state
    assert _SOCKET_CLASS["Established"] == "running"
    assert _SOCKET_CLASS["Connected"] == "running"
    assert _SOCKET_CLASS["Listen"] == "active"


def test_proc_reader_elevates_threads_with_a_checked_target(monkeypatch):
    calls = []
    sample = [{
        "tid": 42,
        "stat": "42 (worker thread) R 1 2 3 0 -1 0 0 0 0 0 100 50 0 0 20 0 2 0 12345 " + "0 " * 20,
        "wchan": "ep_poll\n",
        "status": "voluntary_ctxt_switches:\t12\nnonvoluntary_ctxt_switches:\t3\n",
    }]

    def fake_run(args, callback):
        calls.append(args)
        callback(sample, None)

    monkeypatch.setattr(privileged, "run", fake_run)
    reader = ProcReader(42, starttime=12345)
    errors = []
    reader.elevate(["threads"], errors.append)
    assert errors == [None]
    assert calls == [["read", "42@12345", "threads"]]
    assert reader.elevated["threads"] == sample

    page = SimpleNamespace(reader=reader, _parse_thread=ThreadsPage._parse_thread)
    tasks = ThreadsPage.fetch(page)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["tid"] == 42 and t["name"] == "worker thread" and t["code"] == "R"
    assert t["wchan"] == "ep_poll" and t["ctx"] == 15


def test_proc_reader_does_not_retry_with_a_bare_pid(monkeypatch):
    calls = []

    def fake_run(args, callback):
        calls.append(args)
        callback(None, "invalid pid")

    monkeypatch.setattr(privileged, "run", fake_run)
    reader = ProcReader(42, starttime=12345)
    errors = []
    reader.elevate(["fds"], errors.append)
    assert calls == [["read", "42@12345", "fds"]]
    assert "Update it" in errors[0] and not reader.elevated and not reader.elevated_at


def test_proc_reader_records_when_elevated_data_was_read(monkeypatch):
    monkeypatch.setattr(privileged, "run", lambda _args, callback: callback([[3, "/tmp/x", ""]], None))
    clock = iter([100.0, 250.0])
    monkeypatch.setattr("procular.windows.properties.time.time", lambda: next(clock))
    reader = ProcReader(42, starttime=12345)
    assert reader.elevated_since(("fds",)) is None
    reader.elevate(["fds"], lambda _e: None)
    assert reader.elevated_at == {"fds": 100.0}
    assert reader.elevated_since(("fds", "maps")) == 100.0
    reader.elevate(["fds"], lambda _e: None)  # Refresh: replaces the snapshot and its time
    assert reader.elevated_at == {"fds": 250.0}


def _page(reader, files, refresh_ticks=2):
    page = SimpleNamespace(reader=reader, files=files, refresh_ticks=refresh_ticks, _ticks=0, loads=0,
                           get_mapped=lambda: True)
    page.snapshot_time = lambda: LazyPage.snapshot_time(page)

    def load():  # as LazyPage.load(): counting starts again
        page.loads += 1
        page._ticks = 0

    page.load = load
    return page


def test_lazy_page_does_not_refresh_an_elevated_snapshot_by_itself():
    reader = ProcReader(42, starttime=12345)
    live, snap = _page(reader, ("maps",)), _page(reader, ("fds",))
    reader.elevated["fds"] = []
    reader.elevated_at["fds"] = 100.0
    for _ in range(4):
        LazyPage.tick(live, {})
        LazyPage.tick(snap, {})
    assert live.loads == 2
    assert snap.loads == 0 and snap.snapshot_time() == 100.0


def test_proc_reader_rejects_data_if_the_pid_changes_during_a_read(monkeypatch):
    starts = iter([12345, 12346])
    monkeypatch.setattr(procular_helper, "read_starttime", lambda _pid: next(starts))
    reader = ProcReader(42, starttime=12345)
    with pytest.raises(ProcessLookupError):
        reader.collect(lambda: "unverified data")


def test_about_fade_step_after_destroy():
    dialog = SimpleNamespace(_destroyed=True)
    # must not access self.player or self.volume (cleared by destroy_tree)
    assert AboutDialog._fade_step(dialog) == GLib.SOURCE_REMOVE
