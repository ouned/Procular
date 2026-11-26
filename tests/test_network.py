"""Socket owners found as administrator: valid only while their process is the same."""

import os
import subprocess

from procular.helper.procular_helper import read_starttime
from procular.providers import network


def test_stamp_owners_records_the_start_time():
    child = subprocess.Popen(["sleep", "30"])
    gone = subprocess.Popen(["true"])
    gone.wait()
    try:
        # as the helper sends it: inode as text; processes that exited meanwhile are left out
        owners = network.stamp_owners({"101": child.pid, "102": gone.pid, "bogus": 1, "103": os.getpid()})
        assert owners == {101: (child.pid, read_starttime(child.pid)), 103: (os.getpid(), read_starttime(os.getpid()))}
    finally:
        child.kill()
        child.wait()


def test_owner_is_dropped_when_its_process_is_gone_or_replaced():
    child = subprocess.Popen(["sleep", "30"])
    try:
        owners = network.stamp_owners({101: child.pid, 102: os.getpid()})
        # the same pid, another process (a different start time): not the owner
        owners[103] = (os.getpid(), read_starttime(os.getpid()) + 1)
        assert set(network.valid_owners(owners)) == {101, 102}
    finally:
        child.kill()
        child.wait()
    assert set(network.valid_owners(owners)) == {102}


def test_format_endpoint():
    from procular.widgets.network_view import _format_endpoint

    assert _format_endpoint("192.168.1.1", 443) == "192.168.1.1:443"
    assert _format_endpoint("2001:db8::1", 443) == "[2001:db8::1]:443"
    assert _format_endpoint("2001:db8::1", None) == "2001:db8::1"
    assert _format_endpoint("10.0.0.1", 0) == "10.0.0.1"


def test_traffic_rates():
    rates = network.TrafficRates()
    assert rates.update({1: (100, 1000), 2: (0, 0)}, now=10.0) == {}  # nothing to compare with yet
    # a refresh too soon keeps the rates and the baseline, and learns new sockets
    assert rates.update({1: (150, 1500), 3: (10, 10)}, now=10.2) == {}
    got = rates.update({1: (300, 3000), 2: (0, 0), 3: (30, 10), 4: (5, 5)}, now=12.0)
    assert got == {1: (100.0, 1000.0), 2: (0.0, 0.0), 3: (10.0, 0.0)}  # 4 is new
    # counters that went back: another socket got the inode; no rate for it
    assert rates.update({1: (0, 0)}, now=14.0) == {}
    rates.reset()
    assert rates.update({1: (10, 10)}, now=15.0) == {}


def test_tcp_traffic_sees_own_connection():
    import socket

    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()
    try:
        client.sendall(b"x" * 5000)
        got = b""
        while len(got) < 5000:
            got += conn.recv(65536)
        traffic = network.tcp_traffic()
        if not traffic:  # no netlink here (sandbox)
            return
        inode = os.fstat(client.fileno()).st_ino
        assert traffic[inode][0] in (5000, 5001)  # bytes_acked counts the SYN as one byte
        assert traffic[os.fstat(conn.fileno()).st_ino][1] == 5000
    finally:
        for s in (conn, client, server):
            s.close()


def test_traffic_row_helpers():
    from procular.widgets.network_view import add_traffic, traffic_summary

    rows = [{"inode": 1}, {"inode": 2}, {"inode": 3}]
    traffic = {1: (100, 200), 2: (5, 5)}
    for row in rows:
        add_traffic(row, traffic, {1: (10.0, 2048.0), 2: (0.0, 0.0)})
    assert rows[0] == {"inode": 1, "sent": 100, "received": 200, "send_rate": 10.0, "recv_rate": 2048.0}
    assert rows[2]["recv_rate"] is None and rows[2]["sent"] is None  # UDP: no counters
    assert traffic_summary(rows) == " · ↓ 16.4 kbit/s · ↑ 80 bit/s"
    assert traffic_summary(rows[2:]) == ""


def test_copy_fields():
    from procular.widgets.network_view import COPY_FIELDS, OWNER_COPY_FIELDS

    fields = {name: value for name, _label, value in COPY_FIELDS + OWNER_COPY_FIELDS}
    conn = {"laddr": "2001:db8::1", "lport": 443, "raddr": "192.0.2.7", "rport": 51000, "host": "x.example",
            "process": "nginx", "pid": 42}
    assert {name: value(conn) for name, value in fields.items()} == {
        "copy-local": "[2001:db8::1]:443", "copy-local-ip": "2001:db8::1", "copy-local-port": "443",
        "copy-remote": "192.0.2.7:51000", "copy-remote-ip": "192.0.2.7", "copy-remote-port": "51000",
        "copy-host": "x.example", "copy-process": "nginx", "copy-pid": "42"}
    # listening, as the details window lists it (0.0.0.0 port 0) and as the main list does ("", None);
    # an owner the main list may not know
    for listening in ({"laddr": "0.0.0.0", "lport": 22, "raddr": "0.0.0.0", "rport": 0},
                      {"laddr": "::", "lport": 22, "raddr": "", "rport": None, "process": "Unknown", "pid": None}):
        got = {name: value(listening) for name, value in fields.items()}
        assert got["copy-local"] in ("0.0.0.0:22", "[::]:22")
        assert [n for n, v in got.items() if v is None] == [
            "copy-remote", "copy-remote-ip", "copy-remote-port", "copy-host", "copy-process", "copy-pid"]


def test_copy_actions_follow_the_selection(gtk, monkeypatch):
    from gi.repository import Gio

    from procular.widgets import process_actions

    from procular.widgets.network_view import COPY_FIELDS, add_copy_actions, copy_submenu
    from procular.widgets.table import Column, Table

    table = Table([Column("laddr", "Local", "laddr")], key="inode", menu_model=copy_submenu("t", COPY_FIELDS))
    group = Gio.SimpleActionGroup()
    add_copy_actions(group, table, COPY_FIELDS)
    assert not group.lookup_action("copy-local-ip").get_enabled()  # nothing selected
    table.set_rows([{"inode": 1, "laddr": "127.0.0.1", "lport": 22, "raddr": "0.0.0.0", "rport": 0}])
    table.select_key(1)
    assert group.lookup_action("copy-local-ip").get_enabled()
    assert not group.lookup_action("copy-remote-ip").get_enabled()
    assert group.lookup_action("copy").get_enabled()
    copied = []
    monkeypatch.setattr(process_actions, "copy_text", copied.append)
    group.activate_action("copy-local", None)
    group.activate_action("copy-local-port", None)
    assert copied == ["127.0.0.1:22", "22"]
