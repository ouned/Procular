import json
import os
import signal
import subprocess
import sys

import pytest

from procular.helper import procular_helper as h


def test_rejects_unknown_operation():
    payload, code = h.main(["helper", "rm", "-rf"])
    assert not payload["ok"] and code == 2


@pytest.mark.parametrize("args", [
    ["signal", "abc", "15"],
    ["signal", "-1", "15"],
    ["signal", "1; rm", "15"],
    ["signal", str(os.getpid()), "11"],   # SIGSEGV not allowed
    ["signal", str(os.getpid())],
    ["renice", str(os.getpid()), "-21"],
    ["renice", str(os.getpid()), "20"],
    ["ionice", str(os.getpid()), "4", "0"],
    ["affinity", str(os.getpid()), "99999"],
    ["read", str(os.getpid()), "../../etc/shadow"],
    ["read", str(os.getpid()), "mem"],
    ["close-socket", "1.2.3.4", "80", "not-an-ip", "443"],
    ["close-socket", "1.2.3.4", "99999", "5.6.7.8", "443"],
    ["close-socket", "::1%) or ( sport = :22", "80", "::1", "443"],  # scope id: text for ss's filter
    ["close-socket", "fe80::1%eth0", "80", "::1", "443"],
    ["sockets", "extra"],
    ["handles", "extra"],
    ["oom-adj", str(os.getpid()), "1001"],
    ["oom-adj", str(os.getpid()), "-1001"],
    ["oom-adj", str(os.getpid()), "abc"],
    ["oom-adj", str(os.getpid())],
])
def test_validation(args):
    payload, code = h.main(["helper"] + args)
    assert not payload["ok"]
    assert code == 2


def test_read_own_process_as_user():
    payload, code = h.main(["helper", "read", str(os.getpid()), "status"])
    assert payload["ok"] and code == 0
    import base64
    assert b"Name:" in base64.b64decode(payload["result"]["data"])


def test_signal_zero_impact_on_own_child():
    child = subprocess.Popen(["sleep", "30"])
    try:
        payload, code = h.main(["helper", "signal", str(child.pid), "15"])
        assert payload["ok"]
        assert child.wait(timeout=5) == -15
    finally:
        child.kill()


def test_cli_outputs_json():
    script = os.path.abspath(h.__file__)
    out = subprocess.run([sys.executable, "-I", script, "bogus"], capture_output=True, text=True)
    assert json.loads(out.stdout)["ok"] is False


@pytest.mark.parametrize("args", [
    ["signal", f"{os.getpid()},abc", "15"],
    ["signal", f"{os.getpid()},", "15"],
    ["signal", ",".join([str(os.getpid())] * (h.MAX_PIDS + 1)), "15"],
    ["renice", f"{os.getpid()},-1", "5"],
])
def test_pid_list_validation(args):
    payload, code = h.main(["helper"] + args)
    assert not payload["ok"] and code == 2


def test_signal_pid_list_skips_exited_processes():
    gone = subprocess.Popen(["true"])
    gone.wait()
    children = [subprocess.Popen(["sleep", "30"]) for _ in range(2)]
    try:
        pids = ",".join(str(p) for p in [children[0].pid, gone.pid, children[1].pid])
        payload, code = h.main(["helper", "signal", pids, "15"])
        assert payload["ok"] and code == 0
        assert [c.wait(timeout=5) for c in children] == [-15, -15]
    finally:
        for c in children:
            c.kill()


def test_single_missing_pid_is_an_error():
    gone = subprocess.Popen(["true"])
    gone.wait()
    payload, code = h.main(["helper", "signal", str(gone.pid), "15"])
    assert not payload["ok"] and code == 2


def test_renice_pid_list():
    children = [subprocess.Popen(["sleep", "30"]) for _ in range(2)]
    try:
        payload, _code = h.main(["helper", "renice", ",".join(str(c.pid) for c in children), "7"])
        assert payload["ok"]
        assert [os.getpriority(os.PRIO_PROCESS, c.pid) for c in children] == [7, 7]
    finally:
        for c in children:
            c.kill()


# ---------------------------------------------------------------------------
# signals only reach the process that was meant, not one that reused its pid


def test_read_starttime_matches_scanner():
    from procular.providers.processes import ProcessScanner
    rec = ProcessScanner().scan()[0][os.getpid()]
    assert h.read_starttime(os.getpid()) == rec["starttime"]


def test_read_starttime_of_missing_process():
    child = subprocess.Popen(["true"])
    child.wait()
    assert h.read_starttime(child.pid) is None


def test_signal_if_same_signals_the_right_process():
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert h.signal_if_same(child.pid, h.read_starttime(child.pid), 15)
        assert child.wait(timeout=5) == -15
    finally:
        child.kill()


def test_signal_if_same_spares_a_process_that_reused_the_pid():
    # a different start time is what a recycled pid looks like
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert not h.signal_if_same(child.pid, h.read_starttime(child.pid) + 1, 15)
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.3)
    finally:
        child.kill()
        child.wait()


def test_signal_if_same_on_exited_process():
    child = subprocess.Popen(["true"])
    starttime = h.read_starttime(child.pid)
    child.wait()
    assert not h.signal_if_same(child.pid, starttime, 15)


def test_checked_signal_does_not_fall_back_to_kill_without_pidfd(monkeypatch):
    monkeypatch.delattr(signal, "pidfd_send_signal")
    monkeypatch.setattr(h, "read_starttime", lambda _pid: 42)
    calls = []
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(args))
    assert not h.signal_if_same(10, 41, 15)
    with pytest.raises(OSError, match="pidfd"):
        h.signal_if_same(10, 42, 15)
    assert not calls


@pytest.mark.parametrize("token", ["1@", "1@x", "1@-5", "1@2@3", "@5", "1@" + "9" * 21, "²", "1@²", "٢٣"])
def test_signal_rejects_bad_start_time(token):
    payload, code = h.main(["helper", "signal", token, "15"])
    assert not payload["ok"] and code == 2
    assert payload["error"].startswith("invalid pid")


def test_parse_cpu_list_and_affinity_with_cpu_gaps(monkeypatch):
    assert h.parse_cpu_list("0-2,5,7-8\n") == {0, 1, 2, 5, 7, 8}
    monkeypatch.setattr(h, "possible_cpus", lambda: {0, 2, 3})
    called = []
    monkeypatch.setattr(h, "set_affinity", lambda pid, cpus, _start=None: called.append((pid, cpus)))
    payload, code = h.main(["helper", "affinity", str(os.getpid()), "0,3"])
    assert payload["ok"] and code == 0 and called == [(os.getpid(), {0, 3})]
    payload, code = h.main(["helper", "affinity", str(os.getpid()), "1"])
    assert not payload["ok"] and code == 2


def test_read_and_renice_check_start_time():
    child = subprocess.Popen(["sleep", "30"])
    try:
        start = h.read_starttime(child.pid)
        payload, code = h.main(["helper", "read", f"{child.pid}@{start + 1}", "status"])
        assert not payload["ok"] and code == 2 and "no such process" in payload["error"]
        payload, code = h.main(["helper", "read", f"{child.pid}@{start}", "status"])
        assert payload["ok"] and code == 0
        payload, code = h.main(["helper", "renice", f"{child.pid}@{start + 1}", "5"])
        assert not payload["ok"] and code == 2
        payload, code = h.main(["helper", "renice", f"{child.pid}@{start}", "5"])
        assert payload["ok"] and code == 0
        assert os.getpriority(os.PRIO_PROCESS, child.pid) == 5
    finally:
        child.kill()
        child.wait()


def test_for_each_thread_raises_when_all_threads_disappeared(monkeypatch):
    monkeypatch.setattr(h, "thread_ids", lambda pid: [10, 11])

    def gone(tid):
        raise ProcessLookupError(tid)

    with pytest.raises(ProcessLookupError):
        h.for_each_thread(10, gone)


def test_helper_signal_checks_start_time():
    child = subprocess.Popen(["sleep", "30"])
    try:
        start = h.read_starttime(child.pid)
        payload, code = h.main(["helper", "signal", f"{child.pid}@{start + 1}", "15"])
        assert not payload["ok"] and "no such process" in payload["error"]
        assert child.poll() is None
        payload, code = h.main(["helper", "signal", f"{child.pid}@{start}", "15"])
        assert payload["ok"] and code == 0
        assert child.wait(timeout=5) == -15
    finally:
        child.kill()


def test_helper_signal_several_skips_replaced_ones():
    a = subprocess.Popen(["sleep", "30"])
    b = subprocess.Popen(["sleep", "30"])
    try:
        sa, sb = h.read_starttime(a.pid), h.read_starttime(b.pid)
        payload, _code = h.main(["helper", "signal", f"{a.pid}@{sa + 1},{b.pid}@{sb}", "15"])
        assert payload["ok"]
        assert b.wait(timeout=5) == -15
        assert a.poll() is None
    finally:
        for p in (a, b):
            p.kill()
            p.wait()


# ---------------------------------------------------------------------------
# nice value, I/O priority and affinity belong to threads: all of them have to be changed

_THREADED = ("import threading, time\n"
             "for _ in range(3): threading.Thread(target=time.sleep, args=(30,), daemon=True).start()\n"
             "print('up', flush=True); time.sleep(30)")


def _threaded_child():
    child = subprocess.Popen([sys.executable, "-c", _THREADED], stdout=subprocess.PIPE)
    child.stdout.readline()
    return child


def _per_thread(pid, read):
    return {int(tid): read(int(tid)) for tid in os.listdir(f"/proc/{pid}/task")}


def test_renice_reaches_every_thread():
    child = _threaded_child()
    try:
        payload, _code = h.main(["helper", "renice", str(child.pid), "9"])
        assert payload["ok"]
        nices = _per_thread(child.pid, lambda tid: os.getpriority(os.PRIO_PROCESS, tid))
        assert len(nices) == 4 and set(nices.values()) == {9}
    finally:
        child.kill()
        child.wait()


def test_affinity_reaches_every_thread():
    cpu = min(os.sched_getaffinity(0))
    if cpu >= (os.cpu_count() or 1):
        pytest.skip("CPU numbers with gaps")
    child = _threaded_child()
    try:
        payload, _code = h.main(["helper", "affinity", str(child.pid), str(cpu)])
        assert payload["ok"]
        cpus = _per_thread(child.pid, os.sched_getaffinity)
        assert len(cpus) == 4 and all(c == {cpu} for c in cpus.values())
    finally:
        child.kill()
        child.wait()


def test_ionice_reaches_every_thread():
    psutil = pytest.importorskip("psutil")
    if h.find_tool("ionice") is None:
        pytest.skip("no ionice")
    child = _threaded_child()
    try:
        payload, _code = h.main(["helper", "ionice", str(child.pid), "3", "0"])
        assert payload["ok"], payload
        classes = _per_thread(child.pid, lambda tid: int(psutil.Process(tid).ionice().ioclass))
        assert len(classes) == 4 and set(classes.values()) == {3}
    finally:
        child.kill()
        child.wait()


def test_for_each_thread_of_missing_process():
    gone = subprocess.Popen(["true"])
    gone.wait()
    with pytest.raises(ProcessLookupError):
        h.for_each_thread(gone.pid, lambda tid: None)


def test_for_each_thread_includes_threads_started_meanwhile(monkeypatch):
    listings = iter([[10, 11], [10, 11, 12], [10, 11, 12]])
    monkeypatch.setattr(h, "thread_ids", lambda pid: next(listings))
    seen = []

    def func(tid):
        if tid == 11:
            raise ProcessLookupError  # a thread that ended is skipped
        seen.append(tid)

    h.for_each_thread(10, func)
    assert seen == [10, 12]


def test_thread_mutation_checks_the_process_again_after_enumeration(monkeypatch):
    starts = iter([42, 43])
    monkeypatch.setattr(h, "read_starttime", lambda _pid: next(starts))
    monkeypatch.setattr(h, "thread_ids", lambda _pid: [10, 11])
    seen = []
    with pytest.raises(ProcessLookupError):
        h.for_each_thread(10, seen.append, starttime=42)
    assert seen == []


def test_helper_refuses_when_every_checked_target_was_replaced(monkeypatch):
    monkeypatch.setattr(h, "read_starttime", lambda _pid: 43)
    seen = []
    with pytest.raises(h.UsageError, match="No matching processes"):
        h.for_each_target([(10, 42), (11, 42)], lambda *args: seen.append(args))
    assert seen == []


@pytest.mark.parametrize("name", ["fds", "threads", "exe", "cwd", "root"])
def test_read_special_entries_of_own_process(name):
    # run as root in practice; as the user they work on the user's own processes
    payload, code = h.main(["helper", "read", f"{os.getpid()}@{h.read_starttime(os.getpid())}", name])
    assert payload["ok"] and code == 0
    result = json.loads(json.dumps(payload))["result"]  # what the app receives
    if name == "fds":
        assert all(isinstance(fd, int) and isinstance(target, str) for fd, target, _info in result)
    elif name == "threads":
        assert result[0]["tid"] == os.getpid() and "Name:" in result[0]["status"]
    else:
        assert result.startswith("/")


def test_sockets_maps_inodes_to_pids():
    with __import__("socket").socket() as sock:
        payload, code = h.main(["helper", "sockets"])
        assert payload["ok"] and code == 0
        inode = str(os.fstat(sock.fileno()).st_ino)
        assert payload["result"][inode] == os.getpid()


def _child_adj(pid):
    with open(f"/proc/{pid}/oom_score_adj") as fh:
        return int(fh.read())


def test_set_oom_score_adj_checks_the_process():
    child = subprocess.Popen(["sleep", "30"])
    try:
        start = h.read_starttime(child.pid)
        target = min(1000, max(_child_adj(child.pid), 0) + 100)  # raising it needs no privileges
        h.set_oom_score_adj(child.pid, target, start)
        assert _child_adj(child.pid) == target
        with pytest.raises(ProcessLookupError):  # another process now has the pid
            h.set_oom_score_adj(child.pid, 1000, start + 1)
        assert _child_adj(child.pid) == target
        payload, code = h.main(["helper", "oom-adj", f"{child.pid}@{start}", "1000"])
        assert payload["ok"] and code == 0 and _child_adj(child.pid) == 1000
    finally:
        child.kill()
        child.wait()
    with pytest.raises(ProcessLookupError):
        h.set_oom_score_adj(child.pid, 1000, start)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may lower it")
def test_lowering_oom_score_adj_below_the_minimum_is_a_permission_error():
    child = subprocess.Popen(["sleep", "30"])
    try:
        h.set_oom_score_adj(child.pid, 1000)
        with pytest.raises(PermissionError):
            h.set_oom_score_adj(child.pid, -1000)
    finally:
        child.kill()
        child.wait()


def test_handles_lists_what_own_processes_hold(tmp_path):
    path = tmp_path / "held.txt"
    with open(path, "w") as held:
        payload, code = h.main(["helper", "handles"])
        assert payload["ok"] and code == 0
        mine = payload["result"][str(os.getpid())]
        assert mine["start"] == h.read_starttime(os.getpid())
        assert [held.fileno(), str(path)] in mine["fds"]
        assert mine["exe"] and mine["cwd"] == os.getcwd()
        assert any(executable for _path, executable in mine["maps"])
    assert "2" not in payload["result"]  # kthreadd: kernel threads hold nothing
