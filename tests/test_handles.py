import os
import subprocess

import pytest

from procular.helper.procular_helper import read_starttime
from procular.providers import handles


def test_describe_socket():
    listen = {"proto": "tcp", "laddr": "127.0.0.1", "lport": 5432, "raddr": "0.0.0.0", "rport": 0,
              "state": "Listen"}
    assert handles.describe_socket(listen) == "TCP 127.0.0.1:5432 (listening)"
    conn = {"proto": "tcp6", "laddr": "::1", "lport": 41000, "raddr": "::1", "rport": 5432,
            "state": "Established"}
    assert handles.describe_socket(conn) == "TCP6 [::1]:41000 → [::1]:5432 Established"
    udp = {"proto": "udp", "laddr": "0.0.0.0", "lport": 53, "raddr": "0.0.0.0", "rport": 0, "state": ""}
    assert handles.describe_socket(udp) == "UDP 0.0.0.0:53"


def test_process_entries():
    fds = [(0, "/dev/null"), (3, "socket:[77]"), (4, "socket:[78]"), (5, "/var/log/old.log (deleted)"),
           (6, "pipe:[90]")]
    mapped = [("/usr/bin/app", True), ("/usr/lib/libc.so.6", True), ("/usr/share/fonts/a.ttf", False)]
    entries = handles.process_entries(10, 99, fds, mapped, "/usr/bin/app", "/home/me", "/",
                                      {77: "TCP 0.0.0.0:80 (listening)"})
    got = [(e.fd, e.type, e.name, e.deleted) for e in entries]
    assert got == [
        (None, "Executable", "/usr/bin/app", False),
        (None, "Working directory", "/home/me", False),
        # the root directory is only listed when it is not "/" (a chroot or container)
        (0, "Device", "/dev/null", False),
        (3, "Socket", "TCP 0.0.0.0:80 (listening)", False),
        (4, "Socket", "socket:[78]", False),  # in no table we could read
        (5, "Deleted file", "/var/log/old.log (deleted)", True),
        (6, "Pipe", "pipe:[90]", False),
        # the executable's own mappings are not listed again
        (None, "Library", "/usr/lib/libc.so.6", False),
        (None, "Mapped file", "/usr/share/fonts/a.ttf", False),
    ]
    assert all(e.pid == 10 and e.starttime == 99 for e in entries)


def test_memory_that_the_kernel_calls_deleted_is_no_deleted_file():
    mapped = [("/memfd:wayland-cursor (deleted)", False), ("/SYSV00000000 (deleted)", False),
              ("/dev/zero (deleted)", False), ("/etc/ld.so.cache (deleted)", False)]
    entries = handles.process_entries(1, 1, [(7, "/memfd:pulseaudio (deleted)")], mapped, "", "", "", {})
    assert [(e.type, e.deleted) for e in entries] == [
        ("Memfd", False), ("Memfd", False), ("Shared memory", False), ("Shared memory", False),
        ("Mapped file", True),  # replaced by ldconfig while mapped: the process still uses the old one
    ]


def _index(entries):
    return handles.HandleIndex(entries, {}, set(), 0, 0.0)


def test_search_by_name_and_kind():
    entries = handles.process_entries(1, 1, [(3, "/tmp/Report.txt"), (4, "/tmp/gone (deleted)"), (5, "pipe:[1]")],
                                      [("/usr/lib/libssl.so.3", True)], "", "/tmp", "", {})
    index = _index(entries)
    names = lambda result: [e.name for e in result[0]]  # noqa: E731
    assert names(handles.search(index, "report")) == ["/tmp/Report.txt"]  # ignores case
    assert names(handles.search(index, "", "mapped")) == ["/usr/lib/libssl.so.3"]
    assert names(handles.search(index, "", "deleted")) == ["/tmp/gone (deleted)"]
    assert names(handles.search(index, "/tmp", "files")) == ["/tmp", "/tmp/Report.txt", "/tmp/gone (deleted)"]
    assert names(handles.search(index, "", "pipes")) == ["pipe:[1]"]
    found, total = handles.search(index, "/", limit=2)
    assert len(found) == 2 and total == 4


def test_index_is_reused_while_fresh():
    index = _index([])
    index.built = 100.0
    assert index.fresh(None, now=103.0)
    assert not index.fresh(None, now=100.0 + handles.INDEX_MAX_AGE)
    assert not index.fresh({(1, 1): ()}, now=101.0)  # read as administrator since


def test_build_index_finds_open_file_and_working_directory(tmp_path):
    path = tmp_path / "held.bin"
    path.write_bytes(b"x")
    child = subprocess.Popen(["sleep", "30"], cwd=tmp_path)
    try:
        with open(path, "rb") as held:
            index = handles.build_index()
            me = (os.getpid(), read_starttime(os.getpid()))
            assert me in index.names
            files, _total = handles.search(index, str(path))
            assert [(e.pid, e.fd, e.type) for e in files] == [(os.getpid(), held.fileno(), "File")]
            cwd, _total = handles.search(index, str(tmp_path), "files")
            assert (child.pid, "Working directory") in [(e.pid, e.type) for e in cwd]
            libs, _total = handles.search(index, "libc", "mapped")
            assert any(e.pid == os.getpid() for e in libs)
    finally:
        child.kill()
        child.wait()


def test_build_index_names_own_listening_socket():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        found, _total = handles.search(handles.build_index(), f"127.0.0.1:{port}", "sockets")
        assert [(e.pid, e.fd) for e in found] == [(os.getpid(), server.fileno())]
        assert found[0].name.endswith("(listening)")


def _fake_process(proc, pid, start, fds=(), maps="", exe="/usr/bin/x", denied=False):
    base = proc / str(pid)
    (base / "fd").mkdir(parents=True)
    (base / "stat").write_text(f"{pid} (x) S 1 1 1 0 -1 4194560 " + " ".join(["0"] * 12) + f" {start} 0 0\n")
    for fd, target in fds:
        os.symlink(target, base / "fd" / str(fd))
    (base / "maps").write_text(maps)
    os.symlink(exe, base / "exe")
    os.symlink("/", base / "cwd")
    os.symlink("/", base / "root")
    if denied:
        (base / "fd").chmod(0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads what is denied to users")
def test_build_index_uses_the_administrator_snapshot_for_denied_processes(tmp_path):
    proc = tmp_path / "proc"
    _fake_process(proc, 100, 5000, fds=[(3, "/srv/mine.db")])
    _fake_process(proc, 200, 6000, denied=True)
    _fake_process(proc, 300, 7000, denied=True)
    snapshot = handles.elevated_snapshot({
        "200": {"start": 6000, "fds": [[4, "/srv/theirs.db"]], "maps": [["/usr/lib/libz.so", True]],
                "exe": "/usr/sbin/db", "cwd": "/", "root": "/"},
        "300": {"start": 1, "fds": [[4, "/srv/other.db"]], "maps": [], "exe": "", "cwd": "", "root": ""},
    })
    try:
        plain = handles.build_index(proc=str(proc))
        assert plain.denied == {(200, 6000), (300, 7000)}
        index = handles.build_index(snapshot, proc=str(proc))
        # pid 300 was a different process when the snapshot was taken: still not searched
        assert index.denied == {(300, 7000)} and index.elevated_used == 1
        found, _total = handles.search(index, ".db")
        assert sorted((e.pid, e.fd, e.name) for e in found) == [(100, 3, "/srv/mine.db"), (200, 4, "/srv/theirs.db")]
        assert index.fresh(snapshot) and not index.fresh(None)
    finally:
        for pid in (200, 300):
            (proc / str(pid) / "fd").chmod(0o755)


def test_elevated_snapshot_skips_garbage_and_cleans_paths():
    snapshot = handles.elevated_snapshot({
        "12": {"start": 3, "fds": [[1, "/tmp/caf\udce9"]], "maps": [], "exe": None, "cwd": "/", "root": "/"},
        "x": {"start": 3},
        "13": "nonsense",
    })
    assert snapshot == {(12, 3): ([(1, "/tmp/caf�")], [], "", "/", "/")}
