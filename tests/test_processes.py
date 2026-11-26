import os
import shutil
import subprocess
import sys
import time

from procular.providers.processes import ProcessScanner, binary_name, display_name


def test_display_name():
    assert display_name("avahi-daemon: r", "", "avahi-daemon: running [x.local]") == "avahi-daemon"
    assert display_name("networkd-dispat", "", "/usr/bin/python3", "/usr/bin/networkd-dispatcher") == \
        "networkd-dispatcher"
    assert display_name("gnome-terminal-", "/usr/libexec/gnome-terminal-server", "") == "gnome-terminal-server"
    assert display_name("kworker/0:1", "", "") == "kworker/0:1"


def test_binary_name():
    assert binary_name("Web Content", "/snap/firefox/9181/usr/lib/firefox/firefox", "") == "firefox"
    assert binary_name("procular", "/usr/bin/python3.12", "python3") == "python3.12"
    assert binary_name("x", "/tmp/a.out (deleted)", "") == "a.out"
    # /proc/<pid>/exe of another user's process is not readable: the command line stands in
    assert binary_name("sshd", "", "sshd: alice [priv]") == "sshd"
    assert binary_name("networkd-dispat", "", "/usr/bin/python3") == "python3"
    assert binary_name("ghost", "", "") == "ghost"


def test_binary_names_switch_renames_every_process():
    """Switched between scans, the setting renames idle processes too, whose records the
    scanner would otherwise hand out again unchanged."""
    child = subprocess.Popen([sys.executable, "-c", "import ctypes, time; "
                              "ctypes.CDLL(None).prctl(15, b'Renamed Child', 0, 0, 0); time.sleep(30)"])
    try:
        time.sleep(0.3)
        scanner = ProcessScanner()
        scanner.scan()
        records, _ = scanner.scan()
        assert records[child.pid]["name"] == "Renamed Child"
        scanner.binary_names = True
        records, _ = scanner.scan()
        assert records[child.pid]["name"] == os.path.basename(os.path.realpath(sys.executable))
        assert all(r["name"] for r in records.values())
        scanner.binary_names = False
        records, _ = scanner.scan()
        assert records[child.pid]["name"] == "Renamed Child"
    finally:
        child.kill()
        child.wait()


def test_scan_finds_self_and_computes_deltas():
    scanner = ProcessScanner()
    records, threads = scanner.scan()
    me = records[os.getpid()]
    assert me["uid"] == os.getuid()
    assert me["cmdline"]
    assert me["rss"] > 0
    assert threads >= len(records)
    t = time.time()
    while time.time() - t < 0.2:
        pass  # burn some CPU
    records, _ = scanner.scan()
    me = records[os.getpid()]
    assert me["cpu"] > 0
    assert me["io_read"] is not None  # own process I/O is readable


def test_kernel_threads_flagged():
    """By the kernel's own flag, not as "pid 2 and its children": in a container (where the
    tests may run) or WSL those are ordinary processes."""
    records, _ = ProcessScanner().scan()
    for pid, rec in records.items():
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                flags = int(fh.read().rsplit(b")", 1)[1].split()[6])
        except OSError:
            continue  # gone since the scan
        assert rec["kthread"] == bool(flags & 0x00200000), (pid, rec["name"])
        if rec["kthread"]:
            assert not rec["cmdline"] and not rec["rss"]
    assert not records[os.getpid()]["kthread"]
    assert not records[os.getppid()]["kthread"]


def test_child_of_pid_2_is_no_kernel_thread_by_that(monkeypatch):
    """The stat line of a process whose parent is pid 2, as in a container: an ordinary process."""
    scanner = ProcessScanner()
    real = scanner._read
    me = os.getpid()

    def fake(which, pid, path):
        data = real(which, pid, path)
        if which == 0 and pid == me:
            head, tail = data.rsplit(b")", 1)
            fields = tail.split()
            fields[1] = b"2"  # ppid
            data = head + b") " + b" ".join(fields) + b"\n"
        return data

    monkeypatch.setattr(scanner, "_read", fake)
    records, _ = scanner.scan()
    assert records[me]["ppid"] == 2
    assert not records[me]["kthread"]
    assert records[me]["rss"] > 0


def test_new_child_appears_with_ppid():
    scanner = ProcessScanner()
    child = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.05)
        records, _ = scanner.scan()
        assert records[child.pid]["ppid"] == os.getpid()
        assert records[child.pid]["name"] == "sleep"
    finally:
        child.kill()
        child.wait()


def _r(pid, name, cpu):
    return {"pid": pid, "name": name, "cpu": cpu}


def test_top_cpu_consumer_single_process():
    from procular.providers.processes import top_cpu_consumer
    recs = {1: _r(1, "firefox", 30.0), 2: _r(2, "firefox", 0.5), 3: _r(3, "bash", 2.0)}
    assert top_cpu_consumer(recs) == ("firefox", 1, 30.0, 1)


def test_top_cpu_consumer_group_wins():
    from procular.providers.processes import top_cpu_consumer
    recs = {i: _r(i, "cc1", 6.0) for i in range(10, 22)}
    recs[5] = _r(5, "Web Content", 8.0)
    recs[6] = _r(6, "idle", 0.0)
    name, _pid, total, count = top_cpu_consumer(recs)
    assert (name, count) == ("cc1", 12)
    assert abs(total - 72.0) < 1e-9


def test_top_cpu_consumer_empty():
    from procular.providers.processes import top_cpu_consumer
    assert top_cpu_consumer({}) is None


def test_new_process_cpu_counted_on_first_sight():
    scanner = ProcessScanner()
    scanner.scan()
    proc = subprocess.Popen(["python3", "-c", "import time\nt=time.time()\nwhile time.time()-t<0.4: pass\n"
                             "time.sleep(5)"])
    try:
        time.sleep(0.6)
        records, _ = scanner.scan()
        assert records[proc.pid]["cpu"] > 0
    finally:
        proc.kill()
        proc.wait()


def _idle_child():
    return subprocess.Popen(["sleep", "30"])


def _scan_as_if_later(scanner, monkeypatch, seconds=10):
    """scan() with the wall clock moved ahead, so fresh test processes no longer count as young."""
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)
    try:
        return scanner.scan()[0]
    finally:
        monkeypatch.undo()


def test_idle_process_keeps_its_record(monkeypatch):
    scanner = ProcessScanner()
    child = _idle_child()
    try:
        time.sleep(0.05)
        scans = [_scan_as_if_later(scanner, monkeypatch) for _ in range(4)]
        recs = [s[child.pid] for s in scans]
        # the first scan has no rates yet, and every pid has one refresh scan in 30
        assert sum(a is b for a, b in zip(recs, recs[1:])) >= 2
        assert recs[-1]["cpu"] == 0.0 and recs[-1]["name"] == "sleep"
    finally:
        child.kill()
        child.wait()


def test_changes_are_seen_despite_record_reuse(monkeypatch):
    scanner = ProcessScanner()
    child = _idle_child()
    try:
        time.sleep(0.05)
        for _ in range(3):
            before = _scan_as_if_later(scanner, monkeypatch)[child.pid]
        assert before["state"] == "S"
        os.kill(child.pid, 19)  # SIGSTOP
        time.sleep(0.05)
        after = _scan_as_if_later(scanner, monkeypatch)[child.pid]
        assert after["state"] == "T"
        assert before["state"] == "S"  # the old record was not modified
    finally:
        child.kill()
        child.wait()


def test_descriptors_of_exited_processes_are_closed():
    scanner = ProcessScanner()
    child = _idle_child()
    time.sleep(0.05)
    scanner.scan()
    if scanner._keep_open:
        assert child.pid in scanner._fds[0]
    child.kill()
    child.wait()
    records, _ = scanner.scan()
    assert child.pid not in records
    assert all(child.pid not in fds for fds in scanner._fds)


def test_scan_without_persistent_descriptors():
    scanner = ProcessScanner()
    scanner._keep_open = False
    scanner.scan()
    records, _ = scanner.scan()
    assert records[os.getpid()]["rss"] > 0
    assert not any(scanner._fds)


def test_status_only_fields_stay_current_when_wanted():
    scanner = ProcessScanner()
    scanner.want_status = True
    first = scanner.scan()[0][os.getpid()]["ctx"]
    for _ in range(20):
        time.sleep(0.002)  # every sleep is a voluntary context switch
    assert scanner.scan()[0][os.getpid()]["ctx"] > first


def test_private_memory_never_negative(monkeypatch):
    scanner = ProcessScanner()
    real = scanner._read
    me = os.getpid()

    def fake(which, pid, path):
        if which == 1 and pid == me:
            return b"100 10 15 0 0 0 0\n"  # shared > rss (split-RSS skew)
        return real(which, pid, path)

    monkeypatch.setattr(scanner, "_read", fake)
    records, _ = scanner.scan()
    assert records[me]["private"] == 0


def test_exe_path_that_is_not_utf8_is_displayable(tmp_path):
    exe = os.path.join(os.fsencode(tmp_path), b"sh-\xe9")
    shutil.copy(os.path.realpath("/bin/sh"), exe)
    child = subprocess.Popen([exe, "-c", "read x"], stdin=subprocess.PIPE)
    try:
        time.sleep(0.2)
        records, _threads = ProcessScanner().scan()
        exe_text = records[child.pid]["exe"]
        assert exe_text.endswith("sh-\ufffd")
        exe_text.encode("utf-8")  # GTK takes it
    finally:
        child.kill()
        child.wait()


def test_new_process_io_counted_on_first_sight(tmp_path):
    scanner = ProcessScanner()
    scanner.scan()
    out = tmp_path / "out"
    proc = subprocess.Popen(["python3", "-c", f"open({str(out)!r}, 'wb').write(b'x' * 4_000_000)\n"
                             "import time; time.sleep(5)"])
    try:
        time.sleep(0.6)
        rec = scanner.scan()[0][proc.pid]
        assert rec["io_write"] > 1_000_000 and rec["io_total"] >= rec["io_write"]
    finally:
        proc.kill()
        proc.wait()


def test_a_scan_that_cannot_list_proc_fails_instead_of_reporting_no_processes(monkeypatch):
    import errno

    import pytest

    from procular.providers import processes
    scanner = ProcessScanner()
    scanner.scan()
    last = scanner._last_time

    def full(*_):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(processes.os, "listdir", full)
    with pytest.raises(processes.OutOfDescriptors):
        scanner.scan()
    assert scanner._last_time == last  # the next scan measures from the last good one
    assert not scanner._keep_open and not any(scanner._fds)  # the cached descriptors were let go
    monkeypatch.undo()
    assert os.getpid() in scanner.scan()[0]


def test_running_out_of_descriptors_mid_scan_fails_the_scan(monkeypatch):
    import errno

    import pytest

    from procular.providers import processes
    scanner = ProcessScanner()
    scanner.want_status = True  # status is read for every process

    real_open = processes.os.open

    def open_(path, *args, **kwargs):
        if str(path).endswith("/status"):
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(processes.os, "open", open_)
    with pytest.raises(processes.OutOfDescriptors):
        scanner.scan()


def test_oom_score_and_adjustment():
    scanner = ProcessScanner()
    scanner.want_oom = True
    with open(f"/proc/{os.getpid()}/oom_score_adj") as fh:
        adj = int(fh.read())
    rec = scanner.scan()[0][os.getpid()]
    assert rec["oom_adj"] == adj
    assert isinstance(rec["oom_score"], int)
    child = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.05)
        scanner.scan()
        with open(f"/proc/{child.pid}/oom_score_adj", "w") as fh:
            fh.write(str(min(1000, max(adj, 0) + 100)))
        # without want_oom the value would only be read again on the next staggered refresh
        assert scanner.scan()[0][child.pid]["oom_adj"] == min(1000, max(adj, 0) + 100)
    finally:
        child.kill()
        child.wait()
