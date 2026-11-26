#!/usr/bin/python3 -I
"""Procular privileged helper.

Runs as root via pkexec. It only performs a small whitelist of operations,
validates every argument strictly, never invokes a shell and prints a single
JSON object on stdout. It must only depend on the Python standard library.
"""

import base64
import errno
import ipaddress
import json
import os
import shutil
import signal
import subprocess
import sys

ALLOWED_SIGNALS = {1, 2, 3, 9, 10, 12, 15, 18, 19}
READABLE_FILES = {
    "maps", "smaps", "smaps_rollup", "environ", "io", "limits", "status", "stat",
    "cmdline", "cgroup", "attr/current", "mountinfo", "wchan",
}


class UsageError(Exception):
    pass


def parse_pid(value):
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise UsageError(f"invalid pid: {value!r}")
    pid = int(value)
    if pid <= 0:
        raise UsageError("invalid pid")
    if not os.path.isdir(f"/proc/{pid}"):
        raise UsageError(f"no such process: {pid}")
    return pid


def parse_target(value):
    """PID or PID@STARTTIME -> (pid, starttime or None)."""
    pid_text, sep, start_text = value.partition("@")
    if sep and (not start_text.isascii() or not start_text.isdigit() or len(start_text) > 20):
        raise UsageError(f"invalid pid: {value!r}")
    return parse_pid(pid_text), int(start_text) if sep else None


def parse_cpu_list(text):
    """Linux sysfs CPU list ("0-3,6,8-9") -> set of CPU numbers."""
    cpus = set()
    for part in text.strip().split(","):
        if not part:
            continue
        start, sep, end = part.partition("-")
        if sep:
            cpus.update(range(int(start), int(end) + 1))
        else:
            cpus.add(int(start))
    return cpus


def _read_cpu_list(path):
    try:
        with open(path) as fh:
            cpus = parse_cpu_list(fh.read())
        if cpus:
            return cpus
    except (OSError, ValueError):
        pass
    return None


def online_cpus(path="/sys/devices/system/cpu/online"):
    """Set of online CPU IDs, falling back to 0..cpu_count()-1."""
    cpus = _read_cpu_list(path)
    if cpus is not None:
        return cpus
    return set(range(os.cpu_count() or 1))


def possible_cpus(path="/sys/devices/system/cpu/possible"):
    """Set of valid CPU IDs on this machine (handles offline CPUs with gaps)."""
    cpus = _read_cpu_list(path)
    if cpus is not None:
        return cpus
    return online_cpus()


MAX_PIDS = 4096


def parse_pids(value, parse=parse_pid):
    """PID[,PID...]: one pid must exist; of several, those that exited meanwhile are skipped."""
    parts = value.split(",")
    if len(parts) == 1:
        return [parse(value)]
    if len(parts) > MAX_PIDS:
        raise UsageError("too many pids")
    pids = []
    for part in parts:
        try:
            pids.append(parse(part))
        except UsageError as exc:
            if not str(exc).startswith("no such process"):
                raise
    return pids


def _stat_starttime(data):
    """Field 22 of a /proc/<pid>/stat line: the start time in clock ticks since boot."""
    return int(data[data.rindex(b")") + 2:].split()[19])


def read_starttime(pid):
    """Start time of the process with this pid (clock ticks since boot, /proc/<pid>/stat
    field 22); None if there is none."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        return _stat_starttime(data)
    except (OSError, ValueError, IndexError):
        return None


def require_process(pid, starttime):
    """Reject a PID that no longer belongs to the chosen process."""
    if starttime is not None and read_starttime(pid) != starttime:
        raise ProcessLookupError(f"no such process: {pid}")


def signal_if_same(pid, starttime, sig):
    """Send sig to pid, but only while it is the process that started at starttime.

    Returns False, without sending anything, when that process is gone: exited, or its pid now
    belongs to another process. Raises PermissionError like os.kill. starttime None: no check.

    The pidfd is opened before the check, so check and signal concern the same process even if
    the pid is recycled in between. Do not fall back to kill() for an identity-checked target.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    send = getattr(signal, "pidfd_send_signal", None)
    fd = None
    if pidfd_open is not None and send is not None:
        try:
            fd = pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError as err:
            if starttime is not None and err.errno != errno.ENOSYS:
                raise
    try:
        if starttime is not None and read_starttime(pid) != starttime:
            return False
        if fd is None:
            if starttime is not None:
                raise OSError("Cannot signal safely without pidfd support")
            os.kill(pid, sig)
        else:
            send(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def for_each_target(targets, func):
    """Apply func(pid, starttime) to each target; exited or replaced processes are skipped
    when several were given, or reported as an error when only one was."""
    applied = False
    for pid, starttime in targets:
        if starttime is not None and read_starttime(pid) != starttime:
            if len(targets) == 1:
                raise UsageError(f"no such process: {pid}")
            continue
        try:
            func(pid, starttime)
            applied = True
        except ProcessLookupError:
            if len(targets) == 1:
                raise
    if not applied:
        raise UsageError("No matching processes still exist")


def thread_ids(pid):
    """The ids of the threads of process pid. ProcessLookupError if there is no such process."""
    try:
        return [int(name) for name in os.listdir(f"/proc/{pid}/task")]
    except (FileNotFoundError, NotADirectoryError):
        raise ProcessLookupError(f"no such process: {pid}") from None


def for_each_thread(pid, func, starttime=None):
    """Apply func(tid) to every thread of process pid.

    On Linux the nice value, the I/O priority and the CPU affinity belong to a thread, not to
    the process: the system calls that take a "pid" change the one thread with that id, which
    for a process id is its main thread. A thread started later takes over the values of the
    thread that starts it, so the list is read again until it has no thread that was left out.
    A thread that ends meanwhile is not an error. ProcessLookupError if the process is gone.
    The setters take numeric TIDs (not pidfds); check the process and thread membership again
    immediately before using each TID.
    """
    done = set()
    applied = False
    for _ in range(8):
        require_process(pid, starttime)
        new = [tid for tid in thread_ids(pid) if tid not in done]
        if not new:
            break
        for tid in new:
            done.add(tid)
            require_process(pid, starttime)
            if starttime is not None and not os.path.isdir(f"/proc/{pid}/task/{tid}"):
                continue
            try:
                func(tid)
                applied = True
            except ProcessLookupError:
                pass
    if not applied:
        raise ProcessLookupError(f"no such process: {pid}")


def set_nice(pid, nice, starttime=None):
    """Set the nice value of all threads of a process."""
    for_each_thread(pid, lambda tid: os.setpriority(os.PRIO_PROCESS, tid, nice), starttime)


def set_affinity(pid, cpus, starttime=None):
    """Let all threads of a process run on these CPUs only."""
    for_each_thread(pid, lambda tid: os.sched_setaffinity(tid, cpus), starttime)


OOM_SCORE_ADJ_MIN, OOM_SCORE_ADJ_MAX = -1000, 1000


def set_oom_score_adj(pid, value, starttime=None):
    """Write the OOM score adjustment of process pid, and only while it is the process that
    started at starttime (None: no check).

    The value belongs to the whole process. The /proc/<pid> directory is opened first: what is
    opened through it stays with that process, so if the pid is reused between the check and
    the write, the write fails (ProcessLookupError) instead of reaching the newcomer.
    Raises PermissionError when the value may not be set: another user's process, or a value
    below the lowest an administrator allowed (CAP_SYS_RESOURCE).
    """
    try:
        dirfd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        raise ProcessLookupError(f"no such process: {pid}") from None
    try:
        if starttime is not None:
            fd = os.open("stat", os.O_RDONLY, dir_fd=dirfd)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read()
            try:
                same = _stat_starttime(data) == starttime
            except (ValueError, IndexError):
                same = False
            if not same:
                raise ProcessLookupError(f"no such process: {pid}")
        fd = os.open("oom_score_adj", os.O_WRONLY, dir_fd=dirfd)
        try:
            os.write(fd, str(int(value)).encode("ascii"))
        finally:
            os.close(fd)
    except FileNotFoundError:
        raise ProcessLookupError(f"no such process: {pid}") from None
    finally:
        os.close(dirfd)


def parse_int(value, low, high, what):
    try:
        number = int(value, 10)
    except ValueError:
        raise UsageError(f"invalid {what}: {value!r}") from None
    if not low <= number <= high:
        raise UsageError(f"{what} out of range: {number}")
    return number


def parse_port(value):
    return parse_int(value, 0, 65535, "port")


def parse_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise UsageError(f"invalid address: {value!r}") from None
    # an IPv6 scope id ("fe80::1%eth0") may hold any text, and it would end up in ss's filter
    # expression; /proc/net/tcp6 never has one
    if getattr(ip, "scope_id", None) is not None:
        raise UsageError(f"invalid address: {value!r}")
    return ip


def find_tool(name):
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")


# ---------------------------------------------------------------------------
# operations


def op_signal(args):
    """PID@STARTTIME only signals the process that started then, not one that reused its pid."""
    if len(args) != 2:
        raise UsageError("usage: signal PID[@STARTTIME][,PID[@STARTTIME]...] SIGNAL")
    targets = parse_pids(args[0], parse_target)
    sig = parse_int(args[1], 1, 64, "signal")
    if sig not in ALLOWED_SIGNALS:
        raise UsageError(f"signal not allowed: {sig}")
    sent = [signal_if_same(pid, starttime, sig) for pid, starttime in targets]
    if len(targets) == 1 and not sent[0]:
        raise UsageError(f"no such process: {targets[0][0]}")
    if not any(sent):
        raise UsageError("No matching processes still exist")
    return True


def op_renice(args):
    if len(args) != 2:
        raise UsageError("usage: renice PID[@STARTTIME][,PID[@STARTTIME]...] NICE")
    targets = parse_pids(args[0], parse_target)
    nice = parse_int(args[1], -20, 19, "nice value")
    for_each_target(targets, lambda pid, start: set_nice(pid, nice, start))
    return True


def _ionice(tool, klass, level, tids):
    cmd = [tool, "-c", str(klass)]
    if klass != 3:
        cmd += ["-n", str(level)]
    return subprocess.run(cmd + ["-p"] + [str(tid) for tid in tids], capture_output=True, text=True, timeout=10)


def ionice_process(tool, pid, klass, level, starttime=None):
    """Set the I/O priority of all threads of a process with the ionice tool (see for_each_thread)."""
    done = set()
    applied = False
    for _ in range(8):
        require_process(pid, starttime)
        new = [tid for tid in thread_ids(pid) if tid not in done]
        if not new:
            break
        done.update(new)
        if starttime is not None:
            new = [tid for tid in new if os.path.isdir(f"/proc/{pid}/task/{tid}")]
            require_process(pid, starttime)
            if not new:
                continue
        if _ionice(tool, klass, level, new).returncode == 0:
            applied = True
            continue
        # ionice stops at the first thread it fails on: one by one, to tell a thread that has
        # ended from a real failure
        for tid in new:
            require_process(pid, starttime)
            if starttime is not None and not os.path.isdir(f"/proc/{pid}/task/{tid}"):
                continue
            proc = _ionice(tool, klass, level, [tid])
            if proc.returncode == 0:
                applied = True
            elif os.path.isdir(f"/proc/{pid}/task/{tid}"):
                raise OSError(proc.stderr.strip() or "ionice failed")
    if not applied:
        raise ProcessLookupError(f"no such process: {pid}")


def op_ionice(args):
    if len(args) != 3:
        raise UsageError("usage: ionice PID[@STARTTIME][,PID[@STARTTIME]...] CLASS LEVEL")
    targets = parse_pids(args[0], parse_target)
    klass = parse_int(args[1], 1, 3, "class")
    level = parse_int(args[2], 0, 7, "level")
    tool = find_tool("ionice")
    if not tool:
        raise UsageError("ionice not found")
    for_each_target(targets, lambda pid, start: ionice_process(tool, pid, klass, level, start))
    return True


def op_affinity(args):
    if len(args) != 2:
        raise UsageError("usage: affinity PID[@STARTTIME][,PID[@STARTTIME]...] CPU[,CPU...]")
    targets = parse_pids(args[0], parse_target)
    valid = possible_cpus()
    hi = max(valid)
    cpus = set()
    for part in args[1].split(","):
        cpu = parse_int(part, 0, hi, "cpu")
        if cpu not in valid:
            raise UsageError(f"cpu out of range: {cpu}")
        cpus.add(cpu)
    if not cpus:
        raise UsageError("empty cpu list")
    for_each_target(targets, lambda pid, start: set_affinity(pid, cpus, start))
    return True


def op_oom_adj(args):
    if len(args) != 2:
        raise UsageError("usage: oom-adj PID[@STARTTIME][,PID[@STARTTIME]...] VALUE")
    targets = parse_pids(args[0], parse_target)
    value = parse_int(args[1], OOM_SCORE_ADJ_MIN, OOM_SCORE_ADJ_MAX, "OOM score adjustment")
    for_each_target(targets, lambda pid, start: set_oom_score_adj(pid, value, start))
    return True


def op_read(args):
    if len(args) != 2:
        raise UsageError("usage: read PID[@STARTTIME] NAME")
    pid, starttime = parse_target(args[0])
    if starttime is not None and read_starttime(pid) != starttime:
        raise UsageError(f"no such process: {pid}")
    name = args[1]
    base = f"/proc/{pid}"
    if name == "fds":
        result = []
        for fd in sorted(os.listdir(base + "/fd"), key=int):
            try:
                target = os.readlink(f"{base}/fd/{fd}")
            except OSError:
                continue
            try:
                with open(f"{base}/fdinfo/{fd}", errors="replace") as fh:
                    info = fh.read()
            except OSError:
                info = ""
            result.append([int(fd), target, info])
    elif name in ("exe", "cwd", "root"):
        result = os.readlink(f"{base}/{name}")
    elif name == "threads":
        result = []
        for tid in sorted(os.listdir(base + "/task"), key=int):
            entry = {"tid": int(tid)}
            for fname in ("stat", "wchan", "status"):
                try:
                    with open(f"{base}/task/{tid}/{fname}", "rb") as fh:
                        entry[fname] = fh.read().decode("utf-8", "replace")
                except OSError:
                    entry[fname] = ""
            result.append(entry)
    elif name in READABLE_FILES:
        with open(f"{base}/{name}", "rb") as fh:
            data = fh.read()
        result = {"data": base64.b64encode(data).decode("ascii")}
    else:
        raise UsageError(f"file not allowed: {name!r}")
    if starttime is not None and read_starttime(pid) != starttime:
        raise UsageError(f"no such process: {pid}")
    return result


def op_sockets(args):
    if args:
        raise UsageError("usage: sockets")
    owners = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{name}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                owners.setdefault(target[8:-1], int(name))
    return owners


PF_KTHREAD = 0x00200000


def _mapped_files(base):
    """[(path, executable)] of the files mapped by a process, each path once."""
    found = {}
    with open(f"{base}/maps", "rb") as fh:
        for line in fh:
            parts = line.split(None, 5)
            if len(parts) < 6 or parts[4] == b"0" or not parts[5].startswith(b"/"):
                continue
            path = os.fsdecode(parts[5].rstrip(b"\n"))
            found[path] = found.get(path, False) or b"x" in parts[1]
    return [[path, executable] for path, executable in found.items()]


def op_handles(args):
    """What every process holds: its descriptors, mapped files and exe/cwd/root links, for
    searching them. Kernel threads hold nothing and are left out."""
    if args:
        raise UsageError("usage: handles")
    result = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        base = f"/proc/{name}"
        try:
            with open(f"{base}/stat", "rb") as fh:
                data = fh.read()
            fields = data[data.rindex(b")") + 2:].split()
            if int(fields[6]) & PF_KTHREAD:
                continue
            start = int(fields[19])
            fds = []
            for fd in os.listdir(f"{base}/fd"):
                try:
                    fds.append([int(fd), os.readlink(f"{base}/fd/{fd}")])
                except (OSError, ValueError):
                    continue
        except (OSError, ValueError, IndexError):
            continue  # exited meanwhile
        try:
            maps = _mapped_files(base)
        except OSError:
            maps = []
        entry = {"start": start, "fds": fds, "maps": maps}
        for link in ("exe", "cwd", "root"):
            try:
                entry[link] = os.readlink(f"{base}/{link}")
            except OSError:
                entry[link] = ""
        # still the process that was read: a pid reused meanwhile would mix two processes
        if read_starttime(int(name)) == start:
            result[name] = entry
    return result


def op_close_socket(args):
    if len(args) != 4:
        raise UsageError("usage: close-socket LOCAL_IP LOCAL_PORT REMOTE_IP REMOTE_PORT")
    lip, lport, rip, rport = parse_ip(args[0]), parse_port(args[1]), parse_ip(args[2]), parse_port(args[3])
    tool = find_tool("ss")
    if not tool:
        raise UsageError("ss not found")

    def fmt(ip):
        return f"[{ip}]" if ip.version == 6 else str(ip)

    cmd = [tool, "-K", "src", fmt(lip), "sport", "=", f":{lport}", "dst", fmt(rip), "dport", "=", f":{rport}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise OSError(proc.stderr.strip() or "ss -K failed")
    return proc.stdout.count("\n") > 1


OPERATIONS = {
    "signal": op_signal,
    "renice": op_renice,
    "ionice": op_ionice,
    "affinity": op_affinity,
    "oom-adj": op_oom_adj,
    "read": op_read,
    "sockets": op_sockets,
    "handles": op_handles,
    "close-socket": op_close_socket,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in OPERATIONS:
        return {"ok": False, "error": "unknown operation"}, 2
    try:
        result = OPERATIONS[argv[1]](argv[2:])
    except UsageError as exc:
        return {"ok": False, "error": str(exc)}, 2
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}, 1
    return {"ok": True, "result": result}, 0


if __name__ == "__main__":
    payload, code = main(sys.argv)
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(code)
