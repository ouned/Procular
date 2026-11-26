"""Find what processes hold: open files and directories, sockets, pipes, devices, mapped libraries.

Everything here runs in a worker thread and returns plain data. A search reads every process
once into an index (HandleIndex) and then matches against it, so that typing a query does not
read all of /proc again for every key.
"""

import os
import time

from ..util import linux
from .network import PROTO_FILES
from .processes import PF_KTHREAD

# what the kind filter of a search can ask for: id -> types it includes (None: all of them)
KINDS = {
    "all": None,
    "files": {"File", "Deleted file", "Executable", "Working directory", "Root directory", "Memfd"},
    "mapped": {"Library", "Mapped file", "Shared memory"},
    "sockets": {"Socket"},
    "pipes": {"Pipe"},
    "devices": {"Device"},
    "deleted": None,  # files, libraries and executables that were deleted while open
}
MAX_RESULTS = 5000
INDEX_MAX_AGE = 5.0  # seconds a search may reuse the index of an earlier one


class Entry(tuple):
    """One thing a process holds: (pid, starttime, fd or None, type, name, deleted)."""

    __slots__ = ()

    pid = property(lambda self: self[0])
    starttime = property(lambda self: self[1])
    fd = property(lambda self: self[2])
    type = property(lambda self: self[3])
    name = property(lambda self: self[4])
    deleted = property(lambda self: self[5])


def _endpoint(addr, port):
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def describe_socket(sock):
    """"TCP 127.0.0.1:5432 (listening)", "TCP6 [::1]:41000 → [::1]:5432 Established"."""
    proto = sock["proto"].upper()
    local = _endpoint(sock["laddr"], sock["lport"])
    if sock["state"] == "Listen":
        return f"{proto} {local} (listening)"
    if not sock["rport"]:
        return f"{proto} {local}"
    return f"{proto} {local} → {_endpoint(sock['raddr'], sock['rport'])} {sock['state']}".rstrip()


def read_socket_names(net_dir):
    """{socket inode: description} of one network namespace, from its /proc/<pid>/net."""
    names = {}
    for fname, v6 in PROTO_FILES:
        try:
            text = linux.read_text(f"{net_dir}/{fname}")
        except OSError:
            continue
        for sock in linux.parse_proc_net(text, fname.rstrip("6"), v6):
            names[sock["inode"]] = describe_socket(sock)
    try:
        for inode, path in linux.parse_proc_net_unix(linux.read_text(f"{net_dir}/unix")).items():
            names[inode] = f"Unix {path}" if path else "Unix socket"
    except OSError:
        pass
    return names


# Mappings that the kernel always names "... (deleted)": memory, not files that were deleted
# (memfd_create, System V shared memory, shared anonymous memory, the AIO ring, GPU buffers)
_PSEUDO_FILES = ("/memfd:", "/SYSV", "/dev/zero", "/[", "/drm mm object", "/i915")


def _deleted(name):
    return name.endswith(" (deleted)") and not name.startswith(_PSEUDO_FILES)


def _mapping_type(path, executable):
    if path.startswith("/memfd:"):
        return "Memfd"
    if path.startswith(_PSEUDO_FILES):
        return "Shared memory"
    return "Library" if executable else "Mapped file"


def process_entries(pid, starttime, fds, mapped, exe, cwd, root, sockets):
    """The Entries of one process.

    fds: [(fd, link target)]; mapped: linux.mapped_files(); exe, cwd, root: link targets ("" if
    unknown); sockets: {inode: description}, for the targets that are sockets."""
    out = []
    for type_, path in (("Executable", exe), ("Working directory", cwd), ("Root directory", root)):
        if path and not (type_ == "Root directory" and path == "/"):  # a different root: chroot, container
            out.append(Entry((pid, starttime, None, type_, path, _deleted(path))))
    for fd, target in fds:
        kind = linux.fd_type(target)
        name = target
        if kind == "Socket":
            inode = linux.socket_inode(target)
            name = sockets.get(inode, target) if inode is not None else target
        deleted = kind in ("File", "Device") and _deleted(target)
        if deleted:
            kind = "Deleted file"
        out.append(Entry((pid, starttime, fd, kind, name, deleted)))
    for path, executable in mapped:
        if path == exe:
            continue  # listed as the executable
        out.append(Entry((pid, starttime, None, _mapping_type(path, executable), path, _deleted(path))))
    return out


def _read_links(base):
    result = []
    for name in ("exe", "cwd", "root"):
        try:
            result.append(linux.readlink_text(f"{base}/{name}"))
        except OSError:
            result.append("")
    return result


def _stat_info(base):
    """(starttime, command name, kernel thread) from /proc/<pid>/stat; None if it is gone."""
    try:
        data = linux.read_bytes(f"{base}/stat")
        rpar = data.rindex(b")")
        fields = data[rpar + 2:].split()
        comm = data[data.index(b"(") + 1:rpar].decode("utf-8", "replace")
        return int(fields[19]), comm, bool(int(fields[6]) & PF_KTHREAD)
    except (OSError, ValueError, IndexError):
        return None


class HandleIndex:
    """What every process holds, read in one go.

    entries: list of Entry; names: {(pid, starttime): command name}; denied: {(pid, starttime)}
    of the processes that could not be read (other users'), less those that `elevated` covered;
    elevated_used: how many processes came from the elevated snapshot; built: monotonic time.
    """

    def __init__(self, entries, names, denied, elevated_used, built):
        self.entries = entries
        self.names = names
        self.denied = denied
        self.elevated_used = elevated_used
        self.built = built

    def fresh(self, elevated, now=None):
        now = time.monotonic() if now is None else now
        return now - self.built < INDEX_MAX_AGE and self.elevated is elevated

    elevated = None


def build_index(elevated=None, proc="/proc"):
    """Read every process (kernel threads hold nothing and are skipped).

    elevated: {(pid, starttime): (fds, mapped, exe, cwd, root)} read through the helper; used
    for the processes that cannot be read directly, if they are still the same process."""
    elevated = elevated or {}
    entries = []
    names = {}
    denied = set()
    used = 0
    socket_tables = {}  # network namespace -> {inode: description}
    own_ns = None
    try:
        own_ns = os.readlink(f"{proc}/self/ns/net")
    except OSError:
        pass
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        pid = int(name)
        base = f"{proc}/{name}"
        info = _stat_info(base)
        if info is None or info[2]:
            continue
        starttime, comm, _kthread = info
        key = (pid, starttime)
        try:
            fds = []
            for fd in os.listdir(f"{base}/fd"):
                try:
                    fds.append((int(fd), linux.readlink_text(f"{base}/fd/{fd}")))
                except (OSError, ValueError):
                    continue
        except FileNotFoundError:
            continue  # exited meanwhile
        except OSError:
            snapshot = elevated.get(key)
            if snapshot is None:
                denied.add(key)
                names[key] = comm
                continue
            fds, mapped, exe, cwd, root = snapshot
            sockets = socket_tables.get(own_ns)
            if sockets is None:
                sockets = socket_tables[own_ns] = read_socket_names(f"{proc}/self/net")
            entries += process_entries(pid, starttime, fds, mapped, exe, cwd, root, sockets)
            names[key] = comm
            used += 1
            continue
        try:
            mapped = linux.mapped_files(linux.read_text(f"{base}/maps"))
        except OSError:
            mapped = []
        exe, cwd, root = _read_links(base)
        sockets = {}
        if any(target.startswith("socket:") for _fd, target in fds):
            # a container, or a service with PrivateNetwork=, has sockets of its own
            try:
                ns = os.readlink(f"{base}/ns/net")
            except OSError:
                ns = own_ns
            sockets = socket_tables.get(ns)
            if sockets is None:
                sockets = socket_tables[ns] = read_socket_names(f"{base}/net")
        entries += process_entries(pid, starttime, sorted(fds), mapped, exe, cwd, root, sockets)
        names[key] = comm
    index = HandleIndex(entries, names, denied, used, time.monotonic())
    index.elevated = elevated or None
    return index


def search(index, query, kind="all", limit=MAX_RESULTS):
    """The entries whose name contains query (ignoring case) and whose type fits kind.

    Returns (entries, total): at most `limit` of them, and how many matched."""
    query = query.casefold().strip()
    types = KINDS.get(kind)
    deleted_only = kind == "deleted"
    found = []
    total = 0
    for entry in index.entries:
        if types is not None and entry[3] not in types:
            continue
        if deleted_only and not entry[5]:
            continue
        if query and query not in entry[4].casefold():
            continue
        total += 1
        if len(found) < limit:
            found.append(entry)
    return found, total


def run_search(previous, elevated, query, kind, force=False):
    """Worker job of the Find Open Files window: (index, entries, total).

    The index of the previous search is reused while it is fresh, unless force."""
    index = previous
    if force or index is None or not index.fresh(elevated):
        index = build_index(elevated)
    found, total = search(index, query, kind)
    return index, found, total


def elevated_snapshot(result):
    """The helper's "handles" answer -> {(pid, starttime): (fds, mapped, exe, cwd, root)}.

    Paths in its JSON carry undecodable bytes as surrogates; they are made displayable here."""
    clean = linux.clean_text
    snapshot = {}
    for pid_text, proc in (result or {}).items():
        try:
            key = (int(pid_text), int(proc["start"]))
            fds = [(int(fd), clean(target)) for fd, target in proc.get("fds", ())]
            mapped = [(clean(path), bool(executable)) for path, executable in proc.get("maps", ())]
            links = [clean(proc.get(name) or "") for name in ("exe", "cwd", "root")]
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        snapshot[key] = (fds, mapped, *links)
    return snapshot
