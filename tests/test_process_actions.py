from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from procular.helper import procular_helper
from procular.util import privileged
from procular.widgets import process_actions as pa


@pytest.mark.parametrize("setter, call, request_args", [
    ("set_nice", lambda w, t: pa.renice(w, t, 5), ["renice", "10@99", 5]),
    ("set_thread_affinity", lambda w, t: pa.set_affinity(w, t, {0, 2}), ["affinity", "10@99", "0,2"]),
    ("for_each_thread", lambda w, t: pa.set_ionice(w, t, 2, 4), ["ionice", "10@99", 2, 4]),
])
def test_tuning_keeps_the_same_target_when_elevating(monkeypatch, setter, call, request_args):
    direct = Mock(side_effect=PermissionError)
    monkeypatch.setattr(pa, setter, direct)
    run = Mock()
    monkeypatch.setattr(privileged, "run", run)
    call(SimpleNamespace(toast=Mock()), [(10, 99)])
    assert direct.call_args.args[0] == 10
    assert direct.call_args.args[-1] == 99
    assert run.call_args.args[0] == request_args


def test_renice_does_not_run_when_the_dialogs_pid_was_reused(monkeypatch):
    monkeypatch.setattr(procular_helper, "read_starttime", lambda _pid: 100)
    setter, elevate = Mock(), Mock()
    monkeypatch.setattr(procular_helper.os, "setpriority", setter)
    monkeypatch.setattr(privileged, "run", elevate)
    win = SimpleNamespace(toast=Mock())
    pa.renice(win, [(10, 99)], 5)
    setter.assert_not_called()
    elevate.assert_not_called()
    win.toast.assert_called_once_with("Process no longer exists")


def test_denied_signal_is_not_retried_with_a_bare_pid(monkeypatch):
    monkeypatch.setattr(pa, "signal_if_same", Mock(side_effect=PermissionError))
    calls = []

    def old_helper(args, done):
        calls.append(args)
        done(None, "invalid pid")

    monkeypatch.setattr(privileged, "run", old_helper)
    win = SimpleNamespace(toast=Mock())
    item = SimpleNamespace(pid=10, rec={"starttime": 99})
    pa.send_signal(win, [item], 15)
    assert calls == [["signal", "10@99", 15]]
    assert "Update it" in win.toast.call_args.args[0]
