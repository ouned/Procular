"""Network connections from /proc/net with socket-inode -> pid mapping."""

import os
import socket
import time

from ..helper.procular_helper import read_starttime
from ..util import linux

PROTO_FILES = (("tcp", False), ("tcp6", True), ("udp", False), ("udp6", True))


def socket_owner_map():
    """inode -> pid for every socket we are allowed to see (own processes)."""
    owners = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return owners
    for name in names:
        if not name.isdigit():
            continue
        base = f"/proc/{name}/fd"
        try:
            fds = os.listdir(base)
        except OSError:
            continue
        pid = int(name)
        for fd in fds:
            try:
                target = os.readlink(f"{base}/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inode = linux.socket_inode(target)
                if inode is not None:
                    owners.setdefault(inode, pid)
    return owners


def stamp_owners(owners):
    """{inode: pid} as the helper reports it -> {inode: (pid, start time of that process)}.

    The start time tells later whether the pid still is that process (see valid_owners)."""
    starts = {}
    stamped = {}
    for inode, pid in owners.items():
        try:
            inode, pid = int(inode), int(pid)
        except (TypeError, ValueError):
            continue
        if pid not in starts:
            starts[pid] = read_starttime(pid)
        if starts[pid] is not None:
            stamped[inode] = (pid, starts[pid])
    return stamped


def valid_owners(owners):
    """The entries of {inode: (pid, start time)} whose process still runs: not one that has
    exited, and not another process that got its pid."""
    starts = {}
    valid = {}
    for inode, (pid, start) in owners.items():
        if pid not in starts:
            starts[pid] = read_starttime(pid)
        if starts[pid] == start:
            valid[inode] = (pid, start)
    return valid


def list_connections(extra_owners=None):
    conns = []
    for fname, v6 in PROTO_FILES:
        try:
            text = linux.read_text(f"/proc/net/{fname}")
        except OSError:
            continue
        conns.extend(linux.parse_proc_net(text, fname.rstrip("6"), v6))
    owners = socket_owner_map()
    if extra_owners:
        for inode, pid in extra_owners.items():
            owners.setdefault(int(inode), int(pid))
    for conn in conns:
        conn["pid"] = owners.get(conn["inode"])
        conn["key"] = (conn["proto"], conn["laddr"], conn["lport"], conn["raddr"], conn["rport"], conn["inode"])
    return conns


NETLINK_SOCK_DIAG = 4


def tcp_traffic():
    """{inode: (bytes sent, bytes received)} of every TCP socket, from the kernel's sock_diag.

    Needs no privileges: the kernel reports the counters of all users' sockets. UDP keeps none.
    Empty when netlink is unavailable (e.g. in a sandbox)."""
    out = {}
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_SOCK_DIAG)
    except (OSError, AttributeError):
        return out
    with sock:
        sock.settimeout(2.0)
        for seq, family in enumerate((socket.AF_INET, socket.AF_INET6), 1):
            try:
                sock.sendto(linux.inet_diag_request(family, socket.IPPROTO_TCP, seq), (0, 0))
                while not linux.parse_inet_diag(sock.recv(1 << 16), out):
                    pass
            except OSError:
                continue
    return out


class TrafficRates:
    """Bytes per second of each socket, from two readings of tcp_traffic() apart in time."""

    MIN_INTERVAL = 0.5  # seconds: a refresh sooner than this keeps the rates it had

    def __init__(self):
        self.reset()

    def reset(self):
        """Start over, e.g. when the readings stopped for a while: a rate over the gap would be an
        average that the sockets may not have had at any moment."""
        self._last = {}
        self._when = None
        self.rates = {}  # inode -> (send rate, receive rate)

    def update(self, traffic, now=None):
        now = time.monotonic() if now is None else now
        if self._when is not None and now - self._when < self.MIN_INTERVAL:
            # keep the baseline; just add sockets seen for the first time
            for inode, counts in traffic.items():
                self._last.setdefault(inode, counts)
            return self.rates
        elapsed = None if self._when is None else now - self._when
        rates = {}
        for inode, (sent, received) in traffic.items():
            old = self._last.get(inode)
            if elapsed and old is not None and sent >= old[0] and received >= old[1]:
                rates[inode] = ((sent - old[0]) / elapsed, (received - old[1]) / elapsed)
        self._last = dict(traffic)
        self._when = now
        self.rates = rates
        return rates
