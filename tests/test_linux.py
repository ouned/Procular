import os

from procular.util import linux


def test_parse_cgroup_v2():
    assert linux.parse_cgroup("0::/system.slice/sshd.service\n") == "/system.slice/sshd.service"


def test_parse_cgroup_v1_fallback():
    text = "12:cpu:/foo\n1:name=systemd:/user.slice/x.scope\n"
    assert linux.parse_cgroup(text) == "/user.slice/x.scope"


def test_classify_system_service():
    info = linux.classify_cgroup("/system.slice/NetworkManager.service")
    assert info["unit"] == "NetworkManager.service"
    assert info["unit_scope"] == "system"
    assert info["unit_kind"] == "service"


def test_classify_gnome_app_scope():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Nautilus-1234.scope"
    info = linux.classify_cgroup(path)
    assert info["app_id"] == "org.gnome.Nautilus"
    assert info["launcher"] == "gnome"
    assert info["unit_scope"] == "user"
    assert info["unit_kind"] == "scope"
    assert not info["sandboxed"]


def test_classify_plain_app_scope():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.microsoft.VSCode-990042.scope"
    assert linux.classify_cgroup(path)["app_id"] == "com.microsoft.VSCode"


def test_classify_escaped_app_id():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Text\\x2dEditor-77.scope"
    assert linux.classify_cgroup(path)["app_id"] == "org.gnome.Text-Editor"


def test_classify_flatpak():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.mozilla.firefox-4242.scope"
    info = linux.classify_cgroup(path)
    assert info["sandboxed"] == "flatpak"
    assert info["app_id"] == "org.mozilla.firefox"


def test_classify_snap():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/snap.firefox.firefox-1a2b.scope"
    info = linux.classify_cgroup(path)
    assert info["sandboxed"] == "snap"


def test_classify_user_service():
    path = "/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service"
    info = linux.classify_cgroup(path)
    assert info["unit"] == "pipewire.service"
    assert info["unit_scope"] == "user"


def test_classify_docker_and_podman():
    d = linux.classify_cgroup("/system.slice/docker-0123456789abcdef0123.scope")
    assert d["container"] == "docker:0123456789ab"
    p = linux.classify_cgroup("/machine.slice/libpod-abcdefabcdef1234.scope/container")
    assert p["container"].startswith("podman:")
    conmon = linux.classify_cgroup("/machine.slice/libpod-conmon-abcdefabcdef1234.scope")
    assert not conmon["container"].startswith("podman")


def test_classify_session():
    info = linux.classify_cgroup("/user.slice/user-1000.slice/session-3.scope")
    assert info["session"] == "3"
    assert info["unit"] == "session-3.scope"


def test_decode_caps():
    assert linux.decode_caps("0000000000000022") == ["CAP_DAC_OVERRIDE", "CAP_KILL"]
    assert linux.decode_caps("0") == []
    assert linux.decode_caps("zz") == []


def test_parse_maps_and_modules():
    text = (
        "55d0a0000000-55d0a0002000 r--p 00000000 fd:01 123 /usr/bin/cat\n"
        "55d0a0002000-55d0a0006000 r-xp 00002000 fd:01 123 /usr/bin/cat\n"
        "7f0000000000-7f0000021000 rw-p 00000000 00:00 0 [heap]\n"
        "7f1000000000-7f1000200000 r-xp 00000000 fd:01 456 /usr/lib/libc.so.6\n"
    )
    regions = linux.parse_maps(text)
    assert len(regions) == 4
    assert regions[0]["size"] == 0x2000
    assert regions[2]["path"] == "[heap]"
    mods = linux.modules_from_maps(regions, "/usr/bin/cat")
    assert [m["name"] for m in mods] == ["cat", "libc.so.6"]
    assert mods[0]["is_exe"]
    assert mods[0]["size"] == 0x6000
    assert mods[0]["mapped"] == 0x6000


def test_parse_smaps():
    text = (
        "7f1000000000-7f1000200000 r-xp 00000000 fd:01 456 /usr/lib/libc.so.6\n"
        "Size:               2048 kB\n"
        "Rss:                1024 kB\n"
        "Pss:                 100 kB\n"
        "VmFlags: rd ex mr mw me\n"
        "7f0000000000-7f0000021000 rw-p 00000000 00:00 0 [heap]\n"
        "Rss:                  12 kB\n"
    )
    regions = linux.parse_smaps(text)
    assert regions[0]["Rss"] == 1024 * 1024
    assert regions[0]["Pss"] == 100 * 1024
    assert regions[0]["VmFlags"] == "rd ex mr mw me"
    assert regions[1]["Rss"] == 12 * 1024


def test_parse_environ():
    env = linux.parse_environ(b"HOME=/home/x\0PATH=/bin:/usr/bin\0EMPTY=\0NOEQ\0")
    assert env == [("HOME", "/home/x"), ("PATH", "/bin:/usr/bin"), ("EMPTY", ""), ("NOEQ", "")]


def test_parse_limits():
    text = (
        "Limit                     Soft Limit           Hard Limit           Units     \n"
        "Max cpu time              unlimited            unlimited            seconds   \n"
        "Max open files            1024                 524288               files     \n"
    )
    rows = linux.parse_limits(text)
    assert rows[1] == ("Max open files", "1024", "524288", "files")


def test_fdinfo_and_types():
    info = linux.parse_fdinfo("pos:\t42\nflags:\t02100002\nmnt_id:\t25\n")
    assert info["pos"] == "42"
    flags = linux.describe_open_flags(info["flags"])
    assert "O_RDWR" in flags and "O_CLOEXEC" in flags
    assert linux.fd_type("socket:[1234]") == "Socket"
    assert linux.fd_type("anon_inode:[eventfd]") == "Event"
    assert linux.fd_type("/dev/null") == "Device"
    assert linux.fd_type("/home/x/file") == "File"
    assert linux.socket_inode("socket:[1234]") == 1234
    assert linux.socket_inode("/tmp/x") is None


def test_parse_proc_net_tcp4():
    text = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 31337 1\n"
        "   1: 1C1EA8C0:A6EC 5D37E322:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 4242 1\n"
    )
    socks = linux.parse_proc_net(text, "tcp", False, "little")
    assert socks[0]["laddr"] == "127.0.0.1" and socks[0]["lport"] == 631
    assert socks[0]["state"] == "Listen" and socks[0]["inode"] == 31337
    assert socks[1]["laddr"] == "192.168.30.28"
    assert socks[1]["raddr"] == "34.227.55.93" and socks[1]["rport"] == 443
    assert socks[1]["state"] == "Established" and socks[1]["uid"] == 1000


def test_parse_proc_net_tcp6():
    text = (
        "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000001000000:0277 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555 1\n"
        "   1: 0000000000000000FFFF00000100007F:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 556 1\n"
    )
    socks = linux.parse_proc_net(text, "tcp", True, "little")
    assert socks[0]["laddr"] == "::1" and socks[0]["proto"] == "tcp6"
    assert socks[1]["laddr"] == "127.0.0.1" and socks[1]["lport"] == 8080



def test_parse_proc_net_big_endian():
    # the same sockets as printed by a big-endian kernel (s390x, ppc64): words in network order
    v4 = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 7F000001:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 31337 1\n"
        "   1: C0A81E1C:A6EC 22E3375D:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 4242 1\n"
    )
    socks = linux.parse_proc_net(v4, "tcp", False, "big")
    assert socks[0]["laddr"] == "127.0.0.1" and socks[0]["lport"] == 631
    assert socks[1]["laddr"] == "192.168.30.28"
    assert socks[1]["raddr"] == "34.227.55.93" and socks[1]["rport"] == 443
    v6 = (
        "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000000000000000000000000001:0277 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555 1\n"
        "   1: 20010DB8000000000000000000000042:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 556 1\n"
        "   2: 00000000000000000000FFFF7F000001:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 557 1\n"
    )
    socks = linux.parse_proc_net(v6, "tcp", True, "big")
    assert socks[0]["laddr"] == "::1"
    assert socks[1]["laddr"] == "2001:db8::42" and socks[1]["lport"] == 8080
    assert socks[2]["laddr"] == "127.0.0.1"


def test_parse_proc_net_defaults_to_host_byte_order():
    import struct
    import sys

    word = struct.unpack("=I", bytes([10, 1, 2, 3]))[0]  # how this machine's kernel would print 10.1.2.3
    text = ("header\n"
            f"   0: {word:08X}:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1 1\n")
    assert linux.parse_proc_net(text, "tcp", False)[0]["laddr"] == "10.1.2.3"
    assert linux.parse_proc_net(text, "tcp", False, sys.byteorder)[0]["laddr"] == "10.1.2.3"

def test_kv_kb():
    kv = linux.parse_kv_kb("MemTotal:       1000 kB\nHugePages_Total:       0\n")
    assert kv["MemTotal"] == 1024000
    assert kv["HugePages_Total"] == 0


def test_classify_delegated_subcgroup():
    # systemd-udevd (and other Delegate= services) run in a sub-cgroup of their unit
    info = linux.classify_cgroup("/system.slice/systemd-udevd.service/udev")
    assert info["unit"] == "systemd-udevd.service"
    assert info["unit_scope"] == "system"
    assert info["unit_kind"] == "service"
    nested = linux.classify_cgroup("/system.slice/containerd.service/payload/inner")
    assert nested["unit"] == "containerd.service"


def test_classify_user_manager_and_init_scope():
    mgr = linux.classify_cgroup("/user.slice/user-1000.slice/user@1000.service")
    assert mgr["unit"] == "user@1000.service"
    assert mgr["unit_scope"] == "system"
    init = linux.classify_cgroup("/user.slice/user-1000.slice/user@1000.service/init.scope")
    assert init["unit"] == "init.scope"
    assert init["unit_scope"] == "user"


def test_classify_app_scope_with_subcgroup():
    path = ("/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "app-gnome-org.gnome.Nautilus-1234.scope/worker")
    info = linux.classify_cgroup(path)
    assert info["app_id"] == "org.gnome.Nautilus"
    assert info["unit_scope"] == "user"


def test_effective_state_uses_threads():
    assert linux.effective_state("S", "SSRS") == "R"   # a worker thread runs
    assert linux.effective_state("S", "SDS") == "D"
    assert linux.effective_state("S", "SSS") == "S"
    assert linux.effective_state("S", "") == "S"       # threads unreadable
    assert linux.effective_state("T", "TTR") == "T"    # stopped applies to the whole process
    assert linux.effective_state("Z", "") == "Z"


def test_classify_tells_whose_user_manager_runs_a_unit():
    mine = linux.classify_cgroup("/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service")
    other = linux.classify_cgroup("/user.slice/user-120.slice/user@120.service/session.slice/pipewire.service")
    assert (mine["unit_scope"], mine["unit_id"], mine["unit_uid"]) == ("user", "pipewire.service", 1000)
    assert (other["unit_scope"], other["unit_id"], other["unit_uid"]) == ("user", "pipewire.service", 120)
    system = linux.classify_cgroup("/system.slice/cups.service")
    assert (system["unit_scope"], system["unit_uid"]) == ("system", None)


def test_classify_login_session_is_a_system_unit():
    # session-N.scope lives in user.slice but is a unit of the system manager
    info = linux.classify_cgroup("/user.slice/user-1000.slice/session-3.scope")
    assert (info["unit_scope"], info["unit_kind"], info["unit_uid"]) == ("system", "scope", None)


def test_classify_keeps_the_name_systemd_uses():
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-update\\x2dnotifier-413243.scope"
    info = linux.classify_cgroup(path)
    assert info["unit"] == "app-gnome-update-notifier-413243.scope"  # for display
    assert info["unit_id"] == "app-gnome-update\\x2dnotifier-413243.scope"  # for D-Bus and the services list
    mgr = linux.classify_cgroup("/user.slice/user-1000.slice/user@1000.service")
    assert mgr["unit_id"] == "user@1000.service"


def _diag_reply(inode, sent=None, received=None, seq=1):
    """One SOCK_DIAG_BY_FAMILY message as the kernel sends it, with a tcp_info attribute when
    `sent` is given."""
    import struct

    msg = bytearray(72)
    struct.pack_into("=I", msg, 68, inode)
    if sent is not None:
        info = bytearray(232)
        struct.pack_into("=QQ", info, 120, sent, received)
        msg += struct.pack("=HH", 4 + len(info), linux.INET_DIAG_INFO) + info
    msg += struct.pack("=HH", 8, 5) + bytes(4)  # another attribute, ignored
    return struct.pack("=IHHII", 16 + len(msg), linux.SOCK_DIAG_BY_FAMILY, 2, seq, 0) + msg


def test_parse_inet_diag():
    import struct

    out = {}
    data = _diag_reply(101, 5, 7) + _diag_reply(102) + _diag_reply(0, 1, 1)  # inode 0: time wait
    assert linux.parse_inet_diag(data, out) is False  # more to come
    assert out == {101: (5, 7)}
    done = struct.pack("=IHHII", 20, linux.NLMSG_DONE, 2, 1, 0) + bytes(4)
    assert linux.parse_inet_diag(_diag_reply(103, 2**40, 0) + done, out) is True
    assert out == {101: (5, 7), 103: (2**40, 0)}


def test_parse_inet_diag_error():
    import struct

    import pytest

    error = struct.pack("=IHHIIi", 20, linux.NLMSG_ERROR, 0, 1, 0, -13)
    with pytest.raises(OSError):
        linux.parse_inet_diag(error, {})
    ack = struct.pack("=IHHIIi", 20, linux.NLMSG_ERROR, 0, 1, 0, 0)
    assert linux.parse_inet_diag(ack, {}) is True


def test_inet_diag_request():
    import socket
    import struct

    req = linux.inet_diag_request(socket.AF_INET6, socket.IPPROTO_TCP, seq=7)
    length, mtype, flags, seq, _pid = struct.unpack_from("=IHHII", req)
    assert (length, mtype, seq) == (len(req), linux.SOCK_DIAG_BY_FAMILY, 7) and len(req) == 16 + 56
    assert flags & 0x300  # dump
    assert req[16:18] == bytes((socket.AF_INET6, socket.IPPROTO_TCP))


# --- text that is not UTF-8 (GTK refuses the surrogates Python carries such bytes in) ---


def test_clean_text_replaces_undecodable_bytes():
    assert linux.clean_text("caf\udce9") == "caf�"
    assert linux.clean_text("\ud800 lone") == "? lone"  # a surrogate that stands for no byte
    assert linux.clean_text("Müller") == "Müller"
    assert linux.clean_text(None) is None
    assert linux.clean_text(7) == 7


def test_readlink_and_list_fds_give_valid_utf8(tmp_path):
    name = os.path.join(os.fsencode(tmp_path), b"report-\xff.txt")
    with open(name, "w"):
        pass
    link = os.path.join(os.fsencode(tmp_path), b"link")
    os.symlink(name, link)
    target = linux.readlink_text(os.fsdecode(link))
    assert target.endswith("report-�.txt")
    target.encode("utf-8")

    with open(name) as fh:
        targets = [t for fd, t in linux.list_fds(os.getpid()) if fd == fh.fileno()]
    assert targets and targets[0].endswith("report-�.txt")


def test_systemd_unescape_decodes_utf8():
    assert linux.systemd_unescape(r"media-user-M\xc3\xbcller\x20Stick.mount") == "media-user-Müller Stick.mount"
    assert linux.systemd_unescape(r"dev-disk-by\x2dlabel-x.device") == "dev-disk-by-label-x.device"
    assert linux.systemd_unescape(r"bad\xff.mount") == "bad�.mount"


def test_parse_pressure():
    text = ("some avg10=1.50 avg60=0.75 avg300=0.10 total=123456\n"
            "full avg10=0.20 avg60=0.05 avg300=0.00 total=789\n")
    assert linux.parse_pressure(text) == {
        "some": {"avg10": 1.5, "avg60": 0.75, "avg300": 0.1, "total": 123456},
        "full": {"avg10": 0.2, "avg60": 0.05, "avg300": 0.0, "total": 789},
    }
    # the system-wide cpu file of kernels before 5.13 has no "full" line; junk is skipped
    assert linux.parse_pressure("some avg10=0.00 avg60=0.00 avg300=0.00 total=5\nbogus\n") == {
        "some": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 5}}
    assert linux.parse_pressure("some avg10=x total=y\n") == {}


def test_parse_proc_net_unix():
    text = ("Num       RefCount Protocol Flags    Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 1001 /run/systemd/notify\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 1002 @/tmp/.X11-unix/X0\n"
            "0000000000000000: 00000003 00000000 00000000 0001 03 1003\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 1004 /run/user/1000/my socket\n")
    assert linux.parse_proc_net_unix(text) == {1001: "/run/systemd/notify", 1002: "@/tmp/.X11-unix/X0",
                                              1003: "", 1004: "/run/user/1000/my socket"}


def test_mapped_files():
    text = ("5581a000-5581b000 r--p 00000000 fd:01 100 /usr/bin/cat\n"
            "5581b000-5581c000 r-xp 00001000 fd:01 100 /usr/bin/cat\n"
            "5581d000-5581e000 rw-p 00000000 00:00 0 [heap]\n"
            "7f00a000-7f00b000 r--p 00000000 fd:01 200 /usr/share/locale/locale-archive\n"
            "7f00b000-7f00c000 r-xp 00000000 fd:01 300 /usr/lib/libc.so.6 (deleted)\n"
            "7f00c000-7f00d000 rw-p 00000000 00:00 0\n"
            "7ffc0000-7ffc1000 r-xp 00000000 00:00 0 [vdso]\n")
    assert linux.mapped_files(text) == [("/usr/bin/cat", True), ("/usr/share/locale/locale-archive", False),
                                        ("/usr/lib/libc.so.6 (deleted)", True)]


def test_unit_cgroup():
    assert linux.unit_cgroup("/system.slice/sshd.service", "sshd.service") == "/system.slice/sshd.service"
    # a service with Delegate= keeps its processes in sub-cgroups: the unit's own group counts
    assert linux.unit_cgroup("/system.slice/docker.service/payload", "docker.service") == \
        "/system.slice/docker.service"
    user = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-foo-12.scope"
    assert linux.unit_cgroup(user, "app-gnome-foo-12.scope") == user
    assert linux.unit_cgroup(user, "user@1000.service") == "/user.slice/user-1000.slice/user@1000.service"
    assert linux.unit_cgroup("/system.slice/a.service", "b.service") == ""
    assert linux.unit_cgroup("", "a.service") == ""
