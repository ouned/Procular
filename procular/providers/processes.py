"""Process enumeration straight from /proc (faster than psutil for a full scan)."""

import errno
import os
import pwd
import resource
import time

from ..util import linux

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1
MY_UID = os.getuid()
IS_ROOT = os.geteuid() == 0

# /proc/<pid>/stat "flags": set for threads the kernel runs itself (include/linux/sched.h)
PF_KTHREAD = 0x00200000

_user_cache = {}


def username(uid):
    if uid is None:
        return ""
    name = _user_cache.get(uid)
    if name is None:
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        _user_cache[uid] = name
    return name


def boot_time():
    try:
        with open("/proc/stat", "rb") as fh:
            for line in fh:
                if line.startswith(b"btime"):
                    return int(line.split()[1])
    except OSError:
        pass
    return time.time() - time.monotonic()


BOOT_TIME = boot_time()

_tty_cache = {}


def tty_name(tty_nr):
    if tty_nr == 0:
        return ""
    name = _tty_cache.get(tty_nr)
    if name is None:
        major = (tty_nr >> 8) & 0xFFF
        minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
        if major == 136:
            name = f"pts/{minor}"
        elif major == 4 and minor < 64:
            name = f"tty{minor}"
        elif major == 4:
            name = f"ttyS{minor - 64}"
        else:
            name = f"{major}:{minor}"
        _tty_cache[tty_nr] = name
    return name


def display_name(comm, exe, argv0, argv1=""):
    """Best full name: comm is truncated to 15 chars, so prefer a matching file name."""
    prefix = comm[:15]
    candidates = []
    if exe:
        candidates.append(os.path.basename(exe.replace(" (deleted)", "")))
    if argv0:
        candidates.append(os.path.basename(argv0.split(" ")[0]).rstrip(":"))
    if argv1 and not argv1.startswith("-"):
        candidates.append(os.path.basename(argv1))  # interpreted scripts
    for base in candidates:
        if base and (base.startswith(prefix) or (len(base) >= 4 and prefix.startswith(base))):
            return base
    return comm.rstrip(":")


def binary_name(comm, exe, argv0):
    """Name of the program file a process runs (firefox for Firefox's "Web Content", python3.12
    for a Python script), whatever the process calls itself. Without access to /proc/<pid>/exe
    (other users' processes) the first word of the command line stands in for it."""
    if exe:
        return os.path.basename(exe.replace(" (deleted)", ""))
    base = os.path.basename(argv0.split(" ")[0]).rstrip(":") if argv0 else ""
    return base or comm.rstrip(":")


def process_name(rec, comm, binary=False):
    """The name the Name column shows; see display_name() and binary_name()."""
    if rec["kthread"]:
        return comm
    if binary:
        return binary_name(comm, rec["exe"], rec["argv0"])
    return display_name(comm, rec["exe"], rec["argv0"], rec["argv1"])


class OutOfDescriptors(Exception):
    """The descriptor table is full. A scan then fails as a whole: one that went on would miss
    processes, and the tree would report them as exited. (No OSError: the per-process
    `except OSError` would take it for a process that has exited.)"""

    def __init__(self, err, path):
        super().__init__(f"{os.strerror(err)}: {path}")


def _open(path):
    try:
        return os.open(path, os.O_RDONLY)
    except OSError as err:
        if err.errno in (errno.EMFILE, errno.ENFILE):
            raise OutOfDescriptors(err.errno, path) from None
        raise


def _read(path):
    fd = _open(path)
    try:
        return os.read(fd, 16384)
    finally:
        os.close(fd)


def _field(data, key):
    """Value of 'Key:' in a /proc status-like blob (key given as b"\\nKey:")."""
    i = data.find(key)
    if i < 0:
        return None
    j = data.find(b"\n", i + 1)
    return data[i + len(key):j if j >= 0 else None]


def _kb(data, key):
    v = _field(data, key)
    if v is None:
        return None
    try:
        return int(v.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def _read_number(path):
    """A /proc file holding one integer (oom_score, oom_score_adj); None if unreadable."""
    try:
        return int(_read(path))
    except (OSError, ValueError):
        return None


def _parse_stat(data):
    # pid (comm) state ppid ...  comm may contain spaces and parentheses
    lpar = data.find(b"(")
    rpar = data.rfind(b")")
    comm = data[lpar + 1:rpar].decode("utf-8", "replace")
    fields = data[rpar + 2:].split()
    return comm, fields


def _read_static(pid, rec):
    """Fields that only change on exec / cgroup migration."""
    base = f"/proc/{pid}"
    try:
        exe = linux.readlink_text(base + "/exe")
    except OSError:
        exe = ""
    try:
        raw = linux.read_bytes(base + "/cmdline")
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    except OSError:
        args = []
    try:
        cgroup = linux.parse_cgroup(linux.read_text(base + "/cgroup"))
    except OSError:
        cgroup = ""
    rec["exe"] = exe
    rec["cmdline"] = " ".join(args).replace("\n", " ").replace("\t", " ")
    rec["argv0"] = args[0] if args else ""
    rec["argv1"] = args[1] if len(args) > 1 else ""
    rec["cgroup"] = cgroup
    rec.update(linux.classify_cgroup(cgroup))


# how often the slowly changing data of a process (see scan()) is read again, in scans
_REFRESH_EVERY = 30
# keep the /proc files of every process open between scans if the fd limit allows this many
_WANTED_FDS = 16384


def _raise_fd_limit(wanted):
    """Raise the soft RLIMIT_NOFILE towards `wanted`; return the limit now in effect.

    (Child processes inherit the higher limit, but not the descriptors: Python opens them
    close-on-exec.)
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        return float("inf") if soft == resource.RLIM_INFINITY else soft
    except (OSError, ValueError):
        return 0


def _listdir_proc():
    try:
        return os.listdir("/proc")
    except OSError as err:
        if err.errno in (errno.EMFILE, errno.ENFILE):
            raise OutOfDescriptors(err.errno, "/proc") from None
        raise


class ProcessScanner:
    """Produces a dict pid -> record on each call to scan().

    A scan is kept cheap in three ways:

    * Per process only /proc/<pid>/stat and statm are always read (plus io where it is
      readable). status, the most expensive file, and exe / cmdline / cgroup only change on
      exec, setuid or cgroup migration: they are read for new and young processes, when the
      command name changed, and otherwise every _REFRESH_EVERY scans (staggered by pid). What
      only status has (swap, context switches) is therefore up to that old, unless want_status
      is set. The same goes for the OOM score and its adjustment, unless want_oom is set.
    * Most processes are idle. When the files read the same as last time, the previous record
      is handed out again (the very same dict) instead of being parsed and rebuilt.
    * The files stay open between scans and are re-read with pread(), which saves an open and
      a close per file. The kernel checks permissions on every read, and a descriptor of a
      process that exited fails with ESRCH, so it can never show a process that reused the pid.
    """

    def __init__(self):
        self._prev = {}  # pid -> previous record
        self._raw = {}  # pid -> (stat, statm, io) as read for the previous record
        self._last_time = None
        self._last_uptime = None  # CLOCK_BOOTTIME of the previous scan, comparable to starttime
        self._tick = 0
        self.want_fds = False  # count open files (one listdir per process)
        self.want_status = False  # keep swap / context switches current (they are being shown)
        self.want_oom = False  # keep the OOM score and adjustment current (they are being shown)
        self.binary_names = False  # name processes after their program file (see process_name())
        self._binary_names = False  # what the previous scan named them by
        self.privileged_io = os.geteuid() == 0
        self._fds = ({}, {}, {})  # pid -> open descriptor of its stat / statm / io
        self._keep_open = _raise_fd_limit(_WANTED_FDS) >= _WANTED_FDS

    def _read(self, which, pid, path):
        """Content of a /proc/<pid> file (which: 0 stat, 1 statm, 2 io); OSError if unreadable."""
        if not self._keep_open:
            return _read(path)
        fds = self._fds[which]
        fd = fds.get(pid)
        if fd is not None:
            try:
                return os.pread(fd, 16384, 0)
            except OSError:  # the process is gone; the pid may belong to a new one
                del fds[pid]
                os.close(fd)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as err:
            if err.errno in (errno.EMFILE, errno.ENFILE):
                self._close_fds()
                self._keep_open = False
                return _read(path)
            raise
        try:
            data = os.pread(fd, 16384, 0)
        except OSError:
            os.close(fd)
            raise
        fds[pid] = fd
        return data

    def _close_fds(self, alive=None):
        """Close the descriptors of processes not in `alive` (all of them when None)."""
        for fds in self._fds:
            gone = list(fds) if alive is None else [pid for pid in fds if pid not in alive]
            for pid in gone:
                try:
                    os.close(fds.pop(pid))
                except OSError:
                    pass

    def scan(self):
        """{pid: record}, total threads. Raises (OutOfDescriptors, or OSError when /proc cannot
        be listed) rather than return a list that lacks processes; the next scan then measures
        its rates from the last good one."""
        state = (self._last_time, self._last_uptime)
        try:
            return self._scan()
        except OutOfDescriptors:
            if self._keep_open:  # the descriptors kept open are the likely reason: do without
                self._close_fds()
                self._keep_open = False
            self._last_time, self._last_uptime = state
            raise
        except BaseException:
            self._last_time, self._last_uptime = state
            raise

    def _scan(self):
        names = _listdir_proc()
        now = time.monotonic()
        wall = time.time()
        elapsed = (now - self._last_time) if self._last_time else None
        self._last_time = now
        uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
        born_after = self._last_uptime
        self._last_uptime = uptime
        self._tick += 1
        tick = self._tick
        prev = self._prev
        prev_raw = self._raw
        read = self._read
        privileged_io = self.privileged_io
        want_status = self.want_status
        want_fds = self.want_fds
        want_oom = self.want_oom
        binary_names = self.binary_names
        names_changed = binary_names != self._binary_names  # the setting changed: rename every process
        # an idle process may keep its record unless something is collected that these files don't cover
        may_reuse = (elapsed is not None and not want_status and not want_fds and not want_oom
                     and not names_changed)
        records = {}
        raws = {}
        total_threads = 0

        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            base = "/proc/" + name
            try:
                stat = read(0, pid, base + "/stat")
            except OSError:
                continue
            old = prev.get(pid)
            statm = io = None
            have_files = False  # statm / io already read below

            # ---- unchanged since the last scan?
            if old is not None and may_reuse and (tick + pid) % _REFRESH_EVERY and wall - old["create_time"] >= 5:
                raw = prev_raw[pid]
                if raw[0] == stat:  # same process (start time is in there), nothing ran
                    if not old["kthread"]:
                        try:
                            statm = read(1, pid, base + "/statm")
                        except OSError:
                            continue
                    if old["rchar"] is not None or privileged_io:
                        try:
                            io = read(2, pid, base + "/io")
                        except OSError:
                            pass
                    have_files = True
                    # the old record must not carry rates: they are zero now
                    if statm == raw[1] and io == raw[2] and not (old["cpu"] or old["io_total"] or old["disk_total"]):
                        records[pid] = old
                        raws[pid] = raw
                        total_threads += old["threads"]
                        continue

            try:
                comm, f = _parse_stat(stat)
                state = f[0].decode()
                ppid = int(f[1])
                tty_nr = int(f[4])
                flags = int(f[6])
                minflt = int(f[7])
                majflt = int(f[9])
                utime = int(f[11]) / CLK_TCK
                stime = int(f[12]) / CLK_TCK
                priority = int(f[15])
                nice = int(f[16])
                threads = int(f[17])
                starttime = int(f[19])
                vsize = int(f[20])
                processor = int(f[36]) if len(f) > 36 else -1
                # field 52: only meaningful while the process is a zombie
                exit_code = int(f[49]) if state == "Z" and len(f) > 49 else None
            except (ValueError, IndexError):
                continue
            # not "pid 2 and its children": in a container or WSL those are ordinary processes
            kthread = bool(flags & PF_KTHREAD)
            create_time = BOOT_TIME + starttime / CLK_TCK

            if old is not None and old["starttime"] != starttime:
                old = None  # pid reused
            # new / young processes and after exec; everything else now and then
            refresh = (old is None or old["comm"] != comm or (wall - create_time) < 5
                       or (tick + pid) % _REFRESH_EVERY == 0)

            # memory: kernel threads have none
            rss = shared = 0
            if not kthread:
                try:
                    if not have_files:
                        statm = read(1, pid, base + "/statm")
                    m = statm.split()
                    rss = int(m[1]) * PAGE_SIZE
                    shared = int(m[2]) * PAGE_SIZE  # file backed + shmem
                except (OSError, ValueError, IndexError, AttributeError):
                    continue

            if refresh or want_status:
                try:
                    status = _read(base + "/status")
                except OSError:
                    continue
                try:
                    uids = (_field(status, b"\nUid:") or b"").split()
                    uid = int(uids[0]) if uids else None
                    euid = int(uids[1]) if len(uids) > 1 else uid
                except ValueError:
                    uid = euid = None
                swap = _kb(status, b"\nVmSwap:")
                ctx_vol = _int(_field(status, b"\nvoluntary_ctxt_switches:"))
                ctx_invol = _int(_field(status, b"\nnonvoluntary_ctxt_switches:"))
                capeff = linux.cap_mask((_field(status, b"\nCapEff:") or b"0").strip().decode())
            else:
                uid, euid, swap, capeff = old["uid"], old["euid"], old["swap"], old["capeff"]
                ctx_vol, ctx_invol = old["ctx_vol"], old["ctx_invol"]

            # readable for every process; the score follows memory use, the adjustment is set
            if refresh or want_oom:
                oom_score = _read_number(base + "/oom_score")
                oom_adj = _read_number(base + "/oom_score_adj")
            else:
                oom_score, oom_adj = old["oom_score"], old["oom_adj"]

            # I/O counters (own processes only unless privileged)
            # skip processes we already know we cannot read (saves ~300 failed opens/tick)
            if not have_files and (old is None or old["rchar"] is not None or old["uid"] != uid or privileged_io):
                try:
                    io = read(2, pid, base + "/io")
                except OSError:
                    pass
            if io is not None:
                data = b"\n" + io
                rchar = _int(_field(data, b"\nrchar:")) or 0
                wchar = _int(_field(data, b"\nwchar:")) or 0
                read_bytes = _int(_field(data, b"\nread_bytes:")) or 0
                write_bytes = _int(_field(data, b"\nwrite_bytes:")) or 0
            else:
                rchar = wchar = read_bytes = write_bytes = None

            rec = {
                "pid": pid,
                "ppid": ppid,
                "comm": comm,
                "state": state,
                "tty": tty_name(tty_nr),
                "minflt": minflt,
                "majflt": majflt,
                "cpu_user": utime,
                "cpu_system": stime,
                "cpu_time": utime + stime,
                "priority": priority,
                "nice": nice,
                "threads": threads,
                "starttime": starttime,
                "create_time": create_time,
                "vms": vsize,
                "processor": processor,
                "uid": uid,
                "euid": euid,
                "user": username(uid),
                "rss": rss,
                "private": max(0, rss - shared),  # anonymous memory
                "shared": shared,
                "swap": swap,
                "ctx_vol": ctx_vol,
                "ctx_invol": ctx_invol,
                "ctx": (ctx_vol or 0) + (ctx_invol or 0),
                "capeff": capeff,
                "oom_score": oom_score,
                "oom_adj": oom_adj,
                "kthread": kthread,
                # the kernel shows the exit code only to whom may ptrace the process, as it does
                # /proc/<pid>/io; otherwise it shows 0 (a failed sudo or crontab: not a success)
                "exit_code": exit_code if (IS_ROOT or rchar is not None) else None,
                "rchar": rchar,
                "wchar": wchar,
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
            }
            total_threads += threads

            if refresh:
                _read_static(pid, rec)
                rec["name"] = process_name(rec, comm, binary_names)
            else:
                for key in _STATIC_KEYS:
                    rec[key] = old[key]
                rec["name"] = process_name(rec, comm, binary_names) if names_changed else old["name"]

            # deltas
            if old is not None and elapsed:
                dcpu = rec["cpu_time"] - old["cpu_time"]
                rec["cpu"] = max(0.0, dcpu / elapsed / NCPU * 100.0)
                rec["cpu_kernel"] = max(0.0, (stime - old["cpu_system"]) / elapsed / NCPU * 100.0)
                for key, out in (("rchar", "io_read"), ("wchar", "io_write"),
                                 ("read_bytes", "disk_read"), ("write_bytes", "disk_write")):
                    if rec[key] is not None and old[key] is not None:
                        rec[out] = max(0, rec[key] - old[key]) / elapsed
                    else:
                        rec[out] = None
            else:
                # starttime is truncated to whole clock ticks: one born in the same tick as the
                # last scan began reads up to a tick earlier than that scan
                born = elapsed and born_after is not None and (starttime + 1) / CLK_TCK > born_after
                if born:
                    # started since the last scan: all the CPU time it has used falls into this
                    # interval (otherwise short bursts would show 0% until the next tick)
                    rec["cpu"] = rec["cpu_time"] / elapsed / NCPU * 100.0
                    rec["cpu_kernel"] = stime / elapsed / NCPU * 100.0
                else:
                    rec["cpu"] = 0.0
                    rec["cpu_kernel"] = 0.0
                # the same for its I/O: the counters of a new process start at 0 (a short `cp`
                # would otherwise never show up, and a disk spike would be put down to another one)
                for out, key in (("io_read", "rchar"), ("io_write", "wchar"),
                                 ("disk_read", "read_bytes"), ("disk_write", "write_bytes")):
                    if rec[key] is None:
                        rec[out] = None
                    else:
                        rec[out] = rec[key] / elapsed if born else 0.0
            if rec["io_read"] is not None:
                rec["io_total"] = rec["io_read"] + rec["io_write"]
            else:
                rec["io_total"] = None
            if rec["disk_read"] is not None:
                rec["disk_total"] = rec["disk_read"] + rec["disk_write"]
            else:
                rec["disk_total"] = None

            if want_fds:
                try:
                    rec["fds"] = len(os.listdir(base + "/fd"))
                except OSError:
                    rec["fds"] = None
            else:
                rec["fds"] = old.get("fds") if old else None

            records[pid] = rec
            raws[pid] = (stat, statm, io)

        self._prev = records
        self._raw = raws
        self._binary_names = binary_names  # here, with the records: a failed scan renames nothing
        if self._keep_open:
            self._close_fds(records)
        return records, total_threads


def top_cpu_consumer(records, group_factor=1.5):
    """Who used the most CPU: (name, pid, cpu, count) or None.

    Normally the single busiest process (count 1). When several processes with the same
    name together use clearly more (`group_factor` x the busiest single one), e.g. a
    parallel build or a browser's content processes, the group is reported instead, with
    pid of its busiest member and count = members that used CPU.
    """
    best = None
    groups = {}  # name -> [total, count, busiest rec]
    for rec in records.values():
        cpu = rec["cpu"]
        if best is None or cpu > best["cpu"]:
            best = rec
        if cpu <= 0:
            continue
        g = groups.get(rec["name"])
        if g is None:
            groups[rec["name"]] = [cpu, 1, rec]
        else:
            g[0] += cpu
            g[1] += 1
            if cpu > g[2]["cpu"]:
                g[2] = rec
    if best is None:
        return None
    if groups:
        total, count, rec = max(groups.values(), key=lambda g: g[0])
        if count > 1 and total > group_factor * best["cpu"]:
            return rec["name"], rec["pid"], total, count
    return best["name"], best["pid"], best["cpu"], 1


_STATIC_KEYS = (
    "exe", "cmdline", "argv0", "argv1", "cgroup", "unit", "unit_id", "unit_uid", "unit_scope", "unit_kind",
    "app_id", "launcher", "sandboxed", "container", "session",
)


def _int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
