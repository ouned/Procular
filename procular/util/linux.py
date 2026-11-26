"""Parsers for Linux /proc and cgroup data.

Everything in here is pure (string in, data out) so it can be unit tested and
reused for data returned by the privileged helper.
"""

import ipaddress
import os
import re
import socket
import struct
import sys

# ---------------------------------------------------------------------------
# generic file access


def read_text(path, errors="replace"):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


# Paths and names in /proc are bytes that need not be UTF-8. Python hands them out with lone
# surrogates (os.readlink, os.listdir, psutil, JSON from the helper), which GTK refuses with
# UnicodeEncodeError. What is shown is decoded like everything else from /proc: invalid bytes
# become U+FFFD.

def clean_text(text):
    """text as valid UTF-8: undecodable bytes carried as surrogates become U+FFFD."""
    if not isinstance(text, str):
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # a surrogate that does not stand for a byte (e.g. from JSON)
        return text.encode("utf-8", "replace").decode("utf-8")


def readlink_text(path):
    """os.readlink() as displayable text (see clean_text)."""
    return os.readlink(os.fsencode(path)).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# cgroups / systemd

_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")

_APP_SCOPE_RE = re.compile(
    r"^app-(?:(?P<launcher>gnome|flatpak|kde|xfce|glib|systemd|gnome\\x2dsession|cosmic)-)?"
    r"(?P<id>.+?)(?:-(?P<rand>[0-9a-f]+))?\.scope$"
)
_APP_SERVICE_RE = re.compile(
    r"^app-(?:(?P<launcher>gnome|flatpak|kde|xfce|glib|systemd)-)?"
    r"(?P<id>[^@]+?)(?:@(?P<inst>[^.]*))?\.service$"
)
_CONTAINER_RES = (
    (re.compile(r"^docker-(?P<id>[0-9a-f]{12,})\.scope$"), "docker"),
    (re.compile(r"^libpod-(?!conmon-)(?P<id>[0-9a-f]{12,})\.scope$"), "podman"),
    (re.compile(r"^cri-containerd-(?P<id>[0-9a-f]{12,})\.scope$"), "containerd"),
    (re.compile(r"^crio-(?P<id>[0-9a-f]{12,})\.scope$"), "cri-o"),
    (re.compile(r"^machine-(?P<id>.+)\.scope$"), "machine"),
)


def systemd_unescape(name):
    """systemd's \\xNN escapes back to text; they escape UTF-8 byte by byte ("M\\xc3\\xbcller")."""
    if "\\x" not in name:
        return name
    out = bytearray()
    pos = 0
    for m in _ESCAPE_RE.finditer(name):
        out += name[pos:m.start()].encode("utf-8")
        out.append(int(m.group(1), 16))
        pos = m.end()
    out += name[pos:].encode("utf-8")
    return out.decode("utf-8", "replace")


def parse_cgroup(text):
    """Return the unified (v2) cgroup path, falling back to the systemd v1 hierarchy."""
    fallback = ""
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0" and parts[1] == "":
            return parts[2]
        if parts[1] == "name=systemd":
            fallback = parts[2]
    return fallback


def classify_cgroup(path):
    """Extract systemd/desktop/container information from a cgroup path."""
    info = {
        "unit": "",  # for display: escapes (\x2d) resolved
        "unit_id": "",  # the name systemd knows the unit by
        "unit_uid": None,  # whose user manager runs the unit (unit_scope "user")
        "unit_scope": "",  # "system" | "user" | ""
        "unit_kind": "",  # "service" | "scope" | ""
        "app_id": "",
        "launcher": "",
        "sandboxed": "",  # "flatpak" | "snap" | ""
        "container": "",  # "docker:abcdef123456" ...
        "session": "",
    }
    if not path:
        return info
    comps = [c for c in path.split("/") if c]
    # The owning unit is the deepest .service/.scope component: services with Delegate=
    # (systemd-udevd, container runtimes, ...) put their processes into sub-cgroups.
    unit_idx = -1
    for i in range(len(comps) - 1, -1, -1):
        if comps[i].endswith(".service") or comps[i].endswith(".scope"):
            unit_idx = i
            break
    # every user has a manager of their own (user@<uid>.service): which one says whose unit it is
    manager = next((c for c in comps[:max(unit_idx, 0)] if c.startswith("user@") and c.endswith(".service")), None)

    for comp in comps:
        if comp.startswith("session-") and comp.endswith(".scope"):
            info["session"] = comp[len("session-"):-len(".scope")]
        for regex, kind in _CONTAINER_RES:
            m = regex.match(comp)
            if m:
                info["container"] = f"{kind}:{m.group('id')[:12]}"
        if comp.startswith("lxc.payload."):
            info["container"] = "lxc:" + comp[len("lxc.payload."):]
        if comp.startswith("kubepods"):
            info["container"] = info["container"] or "kubernetes"
        if comp == "docker" or comp == "lxc":
            idx = comps.index(comp)
            if idx + 1 < len(comps):
                info["container"] = f"{comp}:{comps[idx + 1][:12]}"
        if comp.startswith("snap.") and comp.endswith(".scope"):
            info["sandboxed"] = "snap"
            snap = comp[len("snap."):].split(".")
            info["app_id"] = info["app_id"] or (snap[0] + "_" + snap[1].split("-")[0] if len(snap) > 1 else snap[0])

    unit = comps[unit_idx] if unit_idx >= 0 else ""
    if unit.startswith("user@") and unit.endswith(".service"):
        # the user manager itself (systemd --user outside init.scope)
        info["unit"] = info["unit_id"] = unit
        info["unit_kind"] = "service"
        info["unit_scope"] = "system"
    elif unit:
        info["unit"] = systemd_unescape(unit)
        info["unit_id"] = unit
        info["unit_kind"] = "service" if unit.endswith(".service") else "scope"
        if manager is not None:
            info["unit_scope"] = "user"
            uid = manager[len("user@"):-len(".service")]
            info["unit_uid"] = int(uid) if uid.isdigit() else None
        else:
            # also the login sessions in user.slice (session-3.scope): the system manager runs them
            info["unit_scope"] = "system"

    m = _APP_SCOPE_RE.match(unit) or _APP_SERVICE_RE.match(unit)
    if m:
        info["launcher"] = systemd_unescape(m.group("launcher") or "")
        app_id = systemd_unescape(m.group("id"))
        if not app_id.startswith("dbus-") and not app_id.startswith("dbus\\"):
            info["app_id"] = app_id
        if info["launcher"] == "flatpak":
            info["sandboxed"] = "flatpak"
    return info


def unit_cgroup(path, unit_id):
    """The cgroup of the unit `unit_id` that a process in cgroup `path` belongs to: path up to
    the deepest component of that name (the process may sit in a sub-cgroup of its unit, see
    classify_cgroup). "" if the unit is not on the path."""
    if not path or not unit_id:
        return ""
    comps = path.split("/")
    for i in range(len(comps) - 1, -1, -1):
        if comps[i] == unit_id:
            return "/".join(comps[:i + 1]) or "/"
    return ""


# ---------------------------------------------------------------------------
# pressure stall information (/proc/pressure/*, <cgroup>/*.pressure)


def parse_pressure(text):
    """A PSI file -> {"some": {"avg10", "avg60", "avg300", "total"}, "full": {...}}.

    The averages are percent of wall time (over 10, 60 and 300 seconds) in which some task, or
    all non-idle tasks ("full"), waited for the resource; total is that time in microseconds
    since boot. A line that is missing ("full" in the system-wide cpu file before Linux 5.13)
    is left out."""
    result = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        if kind not in ("some", "full"):
            continue
        values = {}
        for field in rest.split():
            key, sep, value = field.partition("=")
            if not sep:
                continue
            try:
                values[key] = int(value) if key == "total" else float(value)
            except ValueError:
                continue
        if "total" in values:
            result[kind] = values
    return result


# ---------------------------------------------------------------------------
# /proc/<pid>/status

CAP_NAMES = [
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_DAC_READ_SEARCH", "CAP_FOWNER", "CAP_FSETID",
    "CAP_KILL", "CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP", "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE", "CAP_NET_BROADCAST", "CAP_NET_ADMIN", "CAP_NET_RAW",
    "CAP_IPC_LOCK", "CAP_IPC_OWNER", "CAP_SYS_MODULE", "CAP_SYS_RAWIO", "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE", "CAP_SYS_PACCT", "CAP_SYS_ADMIN", "CAP_SYS_BOOT", "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE", "CAP_SYS_TIME", "CAP_SYS_TTY_CONFIG", "CAP_MKNOD", "CAP_LEASE",
    "CAP_AUDIT_WRITE", "CAP_AUDIT_CONTROL", "CAP_SETFCAP", "CAP_MAC_OVERRIDE",
    "CAP_MAC_ADMIN", "CAP_SYSLOG", "CAP_WAKE_ALARM", "CAP_BLOCK_SUSPEND", "CAP_AUDIT_READ",
    "CAP_PERFMON", "CAP_BPF", "CAP_CHECKPOINT_RESTORE",
]

SECCOMP_MODES = {"0": "Disabled", "1": "Strict", "2": "Filter"}


def parse_status(text):
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            result[key.strip()] = value.strip()
    return result


def decode_caps(hexmask):
    try:
        mask = int(hexmask, 16)
    except (TypeError, ValueError):
        return []
    names = []
    bit = 0
    while mask:
        if mask & 1:
            names.append(CAP_NAMES[bit] if bit < len(CAP_NAMES) else f"CAP_{bit}")
        mask >>= 1
        bit += 1
    return names


def cap_mask(hexmask):
    try:
        return int(hexmask, 16)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# /proc/<pid>/maps and smaps


def parse_maps(text):
    regions = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        addr, perms, offset, dev, inode = parts[:5]
        path = parts[5] if len(parts) > 5 else ""
        start_s, _, end_s = addr.partition("-")
        try:
            start = int(start_s, 16)
            end = int(end_s, 16)
        except ValueError:
            continue
        regions.append({
            "start": start,
            "end": end,
            "size": end - start,
            "perms": perms,
            "offset": int(offset, 16),
            "dev": dev,
            "inode": int(inode),
            "path": path,
        })
    return regions


_SMAPS_HEADER_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]+ ")


def parse_smaps(text):
    regions = []
    current = None
    for line in text.splitlines():
        if _SMAPS_HEADER_RE.match(line):
            current = parse_maps(line)
            if current:
                current = current[0]
                regions.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value.endswith(" kB"):
            try:
                current[key] = int(value[:-3]) * 1024
            except ValueError:
                pass
        else:
            current[key] = value
    return regions


def parse_kv_kb(text):
    """Parse 'Key:   1234 kB' style files (meminfo, smaps_rollup, status) into bytes."""
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields = value.split()
        if not fields:
            continue
        try:
            number = int(fields[0])
        except ValueError:
            continue
        if len(fields) > 1 and fields[1] == "kB":
            number *= 1024
        result[key.strip()] = number
    return result


def modules_from_maps(regions, exe=None):
    """Collapse file backed mappings into a module list (like loaded DLLs)."""
    modules = {}
    order = []
    for region in regions:
        path = region["path"]
        if not path.startswith("/") or region["inode"] == 0:
            continue
        mod = modules.get(path)
        if mod is None:
            mod = {"path": path, "base": region["start"], "end": region["end"], "mapped": 0}
            modules[path] = mod
            order.append(path)
        mod["base"] = min(mod["base"], region["start"])
        mod["end"] = max(mod["end"], region["end"])
        mod["mapped"] += region["size"]
    result = []
    for path in order:
        mod = modules[path]
        mod["size"] = mod["end"] - mod["base"]
        mod["name"] = os.path.basename(path.replace(" (deleted)", ""))
        mod["deleted"] = path.endswith(" (deleted)")
        mod["is_exe"] = bool(exe) and path == exe
        result.append(mod)
    return result


# ---------------------------------------------------------------------------
# environ / limits / fds


def parse_environ(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    result = []
    for chunk in data.split(b"\0"):
        if not chunk:
            continue
        text = chunk.decode("utf-8", "replace")
        key, sep, value = text.partition("=")
        result.append((key, value if sep else ""))
    return result


def parse_limits(text):
    rows = []
    lines = text.splitlines()
    for line in lines[1:]:
        # columns are fixed width: name(26) soft(21) hard(21) units
        name = line[:26].strip()
        rest = line[26:].split()
        if not name or len(rest) < 2:
            continue
        soft, hard = rest[0], rest[1]
        units = rest[2] if len(rest) > 2 else ""
        rows.append((name, soft, hard, units))
    return rows


def parse_fdinfo(text):
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key in ("pos", "flags", "mnt_id", "ino"):
            result[key] = value.strip()
    return result


_O_FLAGS = (
    (0o1, "O_WRONLY"), (0o2, "O_RDWR"), (0o100, "O_CREAT"), (0o200, "O_EXCL"),
    (0o1000, "O_TRUNC"), (0o2000, "O_APPEND"), (0o4000, "O_NONBLOCK"), (0o4010000, "O_SYNC"),
    (0o40000, "O_DIRECT"), (0o200000, "O_DIRECTORY"), (0o2000000, "O_CLOEXEC"),
    (0o10000000, "O_PATH"),
)


def describe_open_flags(octal_str):
    try:
        flags = int(octal_str, 8)
    except (TypeError, ValueError):
        return ""
    names = []
    if flags & 0o3 == 0:
        names.append("O_RDONLY")
    for bit, name in _O_FLAGS:
        if flags & bit == bit:
            names.append(name)
    return " | ".join(names)


def fd_type(target):
    if target.startswith("socket:"):
        return "Socket"
    if target.startswith("pipe:"):
        return "Pipe"
    if target.startswith("anon_inode:"):
        kind = target[len("anon_inode:"):].strip("[]")
        return {
            "eventfd": "Event", "eventpoll": "Epoll", "timerfd": "Timer",
            "signalfd": "Signal", "inotify": "Inotify", "sync_file": "Sync file",
            "dmabuf": "DMA buffer", "pidfd": "Pidfd", "io_uring": "io_uring",
        }.get(kind, kind or "Anonymous")
    if target.startswith("/dev/"):
        return "Device"
    if target.startswith("/memfd:"):
        return "Memfd"
    if target.startswith("/"):
        return "File"
    if target.startswith("net:") or target.startswith("mnt:") or target.startswith("pid:"):
        return "Namespace"
    return "Other"


def mapped_files(text):
    """The files a process has mapped, from its /proc/<pid>/maps: [(path, executable)], each
    path once, in the order of the first mapping. executable: some mapping of it may run code
    (a program or library)."""
    found = {}
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or parts[4] == "0" or not parts[5].startswith("/"):
            continue  # anonymous memory, [heap], [stack], [vdso], ...
        path = parts[5]
        found[path] = found.get(path, False) or "x" in parts[1]
    return list(found.items())


def socket_inode(target):
    if target.startswith("socket:[") and target.endswith("]"):
        try:
            return int(target[8:-1])
        except ValueError:
            return None
    return None


def list_fds(pid):
    """Return list of (fd, target) for a process. Raises PermissionError."""
    base = f"/proc/{pid}/fd"
    result = []
    for name in os.listdir(base):
        try:
            target = readlink_text(f"{base}/{name}")
        except OSError:
            continue
        result.append((int(name), target))
    result.sort()
    return result


# ---------------------------------------------------------------------------
# /proc/net/{tcp,udp}[6]

TCP_STATES = {
    "01": "Established", "02": "SYN sent", "03": "SYN received", "04": "FIN wait 1",
    "05": "FIN wait 2", "06": "Time wait", "07": "Closed", "08": "Close wait",
    "09": "Last ACK", "0A": "Listen", "0B": "Closing", "0C": "New SYN received",
}


def _decode_addr(hexaddr, v6, byteorder=sys.byteorder):
    host_hex, _, port_hex = hexaddr.partition(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(host_hex)
    # the kernel prints the address as 32-bit words in host byte order (the port is a plain number)
    order = "<" if byteorder == "little" else ">"
    if v6:
        words = struct.unpack(order + "4I", raw)
        packed = struct.pack(">4I", *words)
        addr = ipaddress.IPv6Address(packed)
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped), port
        return str(addr), port
    packed = struct.pack(">I", struct.unpack(order + "I", raw)[0])
    return socket.inet_ntoa(packed), port


def parse_proc_net(text, proto, v6, byteorder=sys.byteorder):
    """Parse /proc/net/tcp-like content. `byteorder` is that of the machine that produced it."""
    result = []
    lines = text.splitlines()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            laddr, lport = _decode_addr(fields[1], v6, byteorder)
            raddr, rport = _decode_addr(fields[2], v6, byteorder)
        except (ValueError, struct.error):
            continue
        state_hex = fields[3].upper()
        if proto == "tcp":
            state = TCP_STATES.get(state_hex, state_hex)
        else:
            state = "Connected" if state_hex == "01" else ("Listen" if rport == 0 else "")
        result.append({
            "proto": proto + ("6" if v6 else ""),
            "laddr": laddr,
            "lport": lport,
            "raddr": raddr,
            "rport": rport,
            "state": state,
            "uid": int(fields[7]),
            "inode": int(fields[9]),
        })
    return result


def parse_proc_net_unix(text):
    """/proc/net/unix -> {inode: path}; "" for an unnamed socket (most connected ones).

    An abstract name is shown with a leading "@" (the kernel prints it like that)."""
    result = {}
    for line in text.splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) < 7:
            continue
        try:
            result[int(fields[6])] = fields[7] if len(fields) > 7 else ""
        except ValueError:
            continue
    return result


# ---------------------------------------------------------------------------
# sock_diag netlink (TCP byte counters)

NLMSG_ERROR = 2
NLMSG_DONE = 3
SOCK_DIAG_BY_FAMILY = 20
INET_DIAG_INFO = 2
_NLMSG_HDR = struct.Struct("=IHHII")  # length, type, flags, sequence, port id
_INET_DIAG_MSG_LEN = 72  # family .. inode (struct inet_diag_msg)
_INET_DIAG_INODE_AT = 68
# offsets in struct tcp_info (linux/tcp.h): tcpi_bytes_acked, tcpi_bytes_received (Linux 4.1+)
_TCPI_BYTES_ACKED_AT = 120
_TCPI_BYTES_RECEIVED_AT = 128


def inet_diag_request(family, protocol, seq=1):
    """A SOCK_DIAG_BY_FAMILY dump request for all sockets of one family/protocol, with tcp_info."""
    # struct inet_diag_req_v2: family, protocol, ext, pad, states, then an all-zero inet_diag_sockid
    body = struct.pack("=BBBxI", family, protocol, 1 << (INET_DIAG_INFO - 1), 0xFFFFFFFF) + bytes(48)
    flags = 0x01 | 0x300  # NLM_F_REQUEST | NLM_F_DUMP
    return _NLMSG_HDR.pack(_NLMSG_HDR.size + len(body), SOCK_DIAG_BY_FAMILY, flags, seq, 0) + body


def parse_inet_diag(data, out):
    """Add {inode: (bytes sent and acknowledged, bytes received)} from one netlink reply buffer to `out`.

    Returns True once the dump is complete (NLMSG_DONE). Raises OSError for an NLMSG_ERROR."""
    pos = 0
    while pos + _NLMSG_HDR.size <= len(data):
        length, mtype, _flags, _seq, _pid = _NLMSG_HDR.unpack_from(data, pos)
        if length < _NLMSG_HDR.size or pos + length > len(data):
            return True  # malformed: stop rather than loop
        if mtype == NLMSG_DONE:
            return True
        if mtype == NLMSG_ERROR:
            err = struct.unpack_from("=i", data, pos + _NLMSG_HDR.size)[0] if length >= _NLMSG_HDR.size + 4 else 0
            if err:
                raise OSError(-err, os.strerror(-err))
            return True
        if mtype == SOCK_DIAG_BY_FAMILY:
            msg = pos + _NLMSG_HDR.size
            end = pos + length
            if msg + _INET_DIAG_MSG_LEN <= end:
                inode = struct.unpack_from("=I", data, msg + _INET_DIAG_INODE_AT)[0]
                attr = msg + _INET_DIAG_MSG_LEN
                while attr + 4 <= end:
                    alen, atype = struct.unpack_from("=HH", data, attr)
                    if alen < 4:
                        break
                    if atype == INET_DIAG_INFO and alen - 4 >= _TCPI_BYTES_RECEIVED_AT + 8 and inode:
                        info = attr + 4
                        out[inode] = (struct.unpack_from("=Q", data, info + _TCPI_BYTES_ACKED_AT)[0],
                                      struct.unpack_from("=Q", data, info + _TCPI_BYTES_RECEIVED_AT)[0])
                    attr += (alen + 3) & ~3
        pos += (length + 3) & ~3
    return False


# ---------------------------------------------------------------------------
# misc


SIGNALS = [
    ("SIGHUP", 1), ("SIGINT", 2), ("SIGQUIT", 3), ("SIGKILL", 9), ("SIGUSR1", 10),
    ("SIGUSR2", 12), ("SIGTERM", 15), ("SIGCONT", 18), ("SIGSTOP", 19),
]

def thread_states(pid):
    """State letters of all threads of a process, e.g. "SSRS" ("" when unreadable)."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return ""
    out = []
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/stat", "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        end = data.rfind(b")")
        out.append(data[end + 2:end + 3].decode("ascii", "replace"))
    return "".join(out)


def effective_state(main, threads):
    """Process state as a user would describe it.

    /proc/<pid>/stat only reports the *main thread*, which in most multi-threaded programs
    waits in a poll loop while worker threads do the work, so a busy process shows
    "Sleeping". A process counts as running when any thread runs, else in disk sleep when
    any thread is; stopped / zombie / dead states apply to the whole process.
    """
    if main in ("T", "t", "Z", "X") or not threads:
        return main
    for state in ("R", "D"):
        if state in threads:
            return state
    return main


STATE_NAMES = {
    "running": "Running", "sleeping": "Sleeping", "disk-sleep": "Disk sleep",
    "stopped": "Stopped", "tracing-stop": "Traced", "zombie": "Zombie", "dead": "Dead",
    "wake-kill": "Wake kill", "waking": "Waking", "idle": "Idle", "locked": "Locked",
    "waiting": "Waiting", "parked": "Parked",
    "R": "Running", "S": "Sleeping", "D": "Disk sleep", "T": "Stopped", "t": "Traced",
    "Z": "Zombie", "X": "Dead", "I": "Idle", "P": "Parked",
}
