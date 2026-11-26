import socket
import sys
from types import SimpleNamespace

from procular.providers.netinfo import (
    fmt_bitrate,
    iface_kind,
    parse_bitrate,
    parse_ipv6_route,
    parse_iw_info,
    parse_iw_link,
    parse_iw_station,
    parse_proc_route,
    parse_proc_wireless,
    parse_resolv_conf,
    signal_quality,
    signal_rating,
    split_addresses,
    wifi_band,
    wifi_channel,
    wifi_standard,
)

IW_INFO = """\
Interface wlp1s0
\tifindex 2
\twdev 0x1
\taddr 04:7b:cb:b7:ed:54
\tssid My Net: 5G
\ttype managed
\twiphy 0
\tchannel 36 (5180 MHz), width: 160 MHz, center1: 5250 MHz
\ttxpower 17.00 dBm
\tmulticast TXQ:
\t\tqsz-byt\tqsz-pkt\tflows\tdrops\tmarks\toverlmt\thashcol\ttx-bytes\ttx-packets
\t\t0\t0\t0\t0\t0\t0\t0\t0\t\t0
"""

IW_STATION = """\
Station 2c:91:ab:57:59:a9 (on wlp1s0)
\tauthorized:\tyes
\tinactive time:\t0 ms
\trx bytes:\t172228736
\ttx retries:\t12
\ttx failed:\t1
\tbeacon loss:\t0
\t\tbeacon rx:\t4833
\tsignal:  \t-61 [-71, -62] dBm
\tsignal avg:\t-59 dBm
\tbeacon signal avg:\t-45 dBm
\ttx bitrate:\t1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 HE-GI 0 HE-DCM 0
\trx bitrate:\t866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\texpected throughput:\t1481.476Mbps
\tconnected time:\t952 seconds
"""

IW_LINK = """\
Connected to 2c:91:ab:57:59:a9 (on wlp1s0)
\tSSID: LoPf
\tfreq: 5180.0
\tsignal: -61 dBm
\trx bitrate: 1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 HE-GI 0 HE-DCM 0
\ttx bitrate: 1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 HE-GI 0 HE-DCM 0
"""


def test_parse_iw_info():
    info = parse_iw_info(IW_INFO)
    assert info == {"ssid": "My Net: 5G", "freq": 5180.0, "channel": 36, "width": 160, "txpower": 17.0,
                    "mode": "managed"}


def test_parse_iw_info_disconnected():
    info = parse_iw_info("Interface wlan0\n\tifindex 3\n\ttype managed\n\ttxpower 3.00 dBm\n")
    assert info["ssid"] is None and info["channel"] is None and info["mode"] == "managed"


def test_parse_iw_station():
    sta = parse_iw_station(IW_STATION)
    assert sta["bssid"] == "2c:91:ab:57:59:a9"
    assert sta["signal"] == -61
    assert sta["signal_avg"] == -59
    assert sta["beacon_signal"] == -45
    assert sta["tx_retries"] == 12 and sta["tx_failed"] == 1 and sta["beacon_loss"] == 0
    assert sta["connected_time"] == 952
    assert sta["expected_throughput"] == 1481.476
    assert sta["tx_bitrate"].startswith("1921.5 MBit/s")


def test_parse_iw_station_empty_or_multiple():
    assert parse_iw_station("") is None
    two = IW_STATION + IW_STATION.replace("2c:91:ab:57:59:a9", "aa:bb:cc:dd:ee:ff").replace("-61", "-80")
    sta = parse_iw_station(two)
    assert sta["bssid"] == "2c:91:ab:57:59:a9" and sta["signal"] == -61


def test_parse_iw_link():
    link = parse_iw_link(IW_LINK)
    assert link["ssid"] == "LoPf" and link["freq"] == 5180.0 and link["signal"] == -61
    assert parse_iw_link("Not connected.\n") is None


def test_parse_bitrate():
    assert parse_bitrate("1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 HE-GI 0 HE-DCM 0") == \
        {"rate": 1921.5, "mcs": 9, "nss": 2, "width": 160}
    assert parse_bitrate("866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2") == \
        {"rate": 866.7, "mcs": 9, "nss": 2, "width": 80}
    # 802.11n: stream count is encoded in the MCS index
    assert parse_bitrate("300.0 MBit/s MCS 15 40MHz short GI") == {"rate": 300.0, "mcs": 15, "nss": 2, "width": 40}
    assert parse_bitrate("54.0 MBit/s") == {"rate": 54.0, "mcs": None, "nss": None, "width": None}
    assert parse_bitrate(None) is None


def test_fmt_bitrate():
    assert fmt_bitrate(parse_bitrate("1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2")) == "1922 Mbit/s (MCS 9, 2 streams)"
    assert fmt_bitrate(parse_bitrate("6.0 MBit/s")) == "6 Mbit/s"
    assert fmt_bitrate(None) == "—"


def test_wifi_standard():
    assert wifi_standard(["2882.4 MBit/s 160MHz EHT-MCS 11 EHT-NSS 2"]) == "Wi-Fi 7 (802.11be)"
    assert wifi_standard(["1921.5 MBit/s HE-MCS 9 HE-NSS 2"], 5180) == "Wi-Fi 6 (802.11ax)"
    assert wifi_standard(["1921.5 MBit/s HE-MCS 9 HE-NSS 2"], 6115) == "Wi-Fi 6E (802.11ax)"
    assert wifi_standard([None, "866.7 MBit/s VHT-MCS 9 80MHz VHT-NSS 2"]) == "Wi-Fi 5 (802.11ac)"
    assert wifi_standard(["300.0 MBit/s MCS 15 40MHz short GI"]) == "Wi-Fi 4 (802.11n)"
    assert wifi_standard(["54.0 MBit/s"], 2437) == "802.11g"
    assert wifi_standard([None, None]) is None


def test_band_and_channel():
    assert (wifi_band(2437), wifi_channel(2437)) == ("2.4 GHz", 6)
    assert wifi_channel(2484) == 14
    assert (wifi_band(5180), wifi_channel(5180)) == ("5 GHz", 36)
    assert (wifi_band(5955), wifi_channel(5955)) == ("6 GHz", 1)
    assert wifi_band(None) is None and wifi_channel(None) is None


def test_signal_quality_and_rating():
    assert signal_quality(-100) == 0 and signal_quality(-50) == 100 and signal_quality(-30) == 100
    assert signal_quality(-75) == 50
    assert signal_quality(None) is None
    assert signal_rating(-45) == "excellent"
    assert signal_rating(-65) == "fair"
    assert signal_rating(-90) == "very weak"


def test_parse_proc_wireless():
    text = """\
Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp1s0: 0000   48.  -62.  -256        0      0      0      0      1        0
"""
    assert parse_proc_wireless(text) == {"wlp1s0": {"quality": 48.0, "signal": -62}}


def test_parse_proc_route():
    text = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp1s0\t00000000\t011EA8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
wlp1s0\t001EA8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
"""
    assert parse_proc_route(text, "little") == {"wlp1s0": ("192.168.30.1", 600), "eth0": ("192.168.0.1", 100)}


def test_parse_proc_route_big_endian():
    text = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp1s0\t00000000\tC0A81E01\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\tC0A80001\t0003\t0\t0\t100\t00000000\t0\t0\t0
wlp1s0\tC0A81E00\t00000000\t0001\t0\t0\t600\tFFFFFF00\t0\t0\t0
"""
    assert parse_proc_route(text, "big") == {"wlp1s0": ("192.168.30.1", 600), "eth0": ("192.168.0.1", 100)}


def test_parse_proc_route_defaults_to_host_byte_order():
    gw = int.from_bytes(bytes([10, 0, 0, 1]), sys.byteorder)  # how this machine's kernel would print 10.0.0.1
    text = f"header\neth0\t00000000\t{gw:08X}\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    assert parse_proc_route(text) == {"eth0": ("10.0.0.1", 100)}


def test_parse_ipv6_route():
    zero = "0" * 32
    text = (
        f"2a028070d18732e00000000000000000 40 {zero} 00 {zero} 00000258 0000000f 00000000 00000001 wlp1s0\n"
        f"{zero} 00 {zero} 00 fe800000000000002e91abfffe5759a6 00000258 00000001 00000000 00000003 wlp1s0\n"
        f"{zero} 00 {zero} 00 {zero} ffffffff 00000001 00000000 00200200 lo\n"
    )
    assert parse_ipv6_route(text) == {"wlp1s0": ("fe80::2e91:abff:fe57:59a6", 600)}


def test_parse_resolv_conf():
    servers, search = parse_resolv_conf("# x\nnameserver 1.1.1.1\nnameserver ::1 # local\nsearch a.b c.d\n")
    assert servers == ["1.1.1.1", "::1"] and search == ["a.b", "c.d"]


def test_split_addresses():
    A = SimpleNamespace
    addrs = [
        A(family=socket.AF_INET6, address="fe80::1%wlan0", netmask="ffff:ffff:ffff:ffff::"),
        A(family=socket.AF_INET, address="192.168.1.5", netmask="255.255.255.0"),
        A(family=socket.AF_INET6, address="fd00::5", netmask="ffff:ffff:ffff:ffff::"),
        A(family=socket.AF_INET6, address="2001:db8::5", netmask="ffff:ffff:ffff:ffff::"),
    ]
    v4, v6 = split_addresses(addrs)
    assert v4 == ["192.168.1.5/24"]
    assert v6 == ["2001:db8::5/64", "fd00::5/64", "fe80::1/64"]


def test_iface_kind(tmp_path):
    def make(name, files=(), dirs=(), uevent="", arptype=None):
        base = tmp_path / name
        base.mkdir()
        for d in dirs:
            (base / d).mkdir()
        for f in files:
            (base / f).write_text("")
        (base / "uevent").write_text(uevent)
        if arptype is not None:
            (base / "type").write_text(f"{arptype}\n")

    make("wlan0", dirs=("wireless", "device"), uevent="DEVTYPE=wlan\n", arptype=1)
    make("eth0", dirs=("device",), arptype=1)
    make("br0", dirs=("bridge",), uevent="DEVTYPE=bridge\n", arptype=1)
    make("veth1", arptype=1)
    make("tun0", files=("tun_flags",), arptype=65534)
    make("wg0", uevent="DEVTYPE=wireguard\n", arptype=65534)
    kinds = {n: iface_kind(n, str(tmp_path)) for n in ("wlan0", "eth0", "br0", "veth1", "tun0", "wg0")}
    assert kinds == {"wlan0": "Wi-Fi", "eth0": "Ethernet", "br0": "Bridge", "veth1": "Virtual Ethernet",
                     "tun0": "TUN/TAP", "wg0": "WireGuard"}


def test_wifi_quality_from_proc_wireless(tmp_path):
    from procular.providers.netinfo import NetInfoReader
    from procular.providers.system import History
    from procular.util.timeline import SampleTime
    path = tmp_path / "wireless"
    path.write_text("Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
                    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
                    "wlp1s0: 0000   41.  -69.  -256        0      0      0      0      3        0\n"
                    "wlan1: 0000    0.    0.    0.        0      0      0      0      0        0\n")
    reader = NetInfoReader()
    quality = reader.wifi_quality(str(path))
    assert quality == {"wlp1s0": 62.0, "wlan1": 0}
    assert reader.wifi_quality(str(tmp_path / "missing")) == {}
    # the signal history is recorded from this alone, without the detailed interface data
    history = History(size=10)
    snap = {"time": SampleTime(0, 0), "cpu_total": 0, "cpu_kernel": 0, "cpu_cores": [], "mem_used": 0, "mem_cache": 0,
            "swap_used": 0, "io_read": 0, "io_write": 0, "disk_read": 0, "disk_write": 0, "net_rx": 0,
            "net_tx": 0, "gpu": None, "gpu_mem": None, "disks": {}, "nics": {}, "net_info": None,
            "wifi_quality": quality}
    history.push(snap, None, None, None)
    history.push(snap, None, None, None)
    assert list(history.wifi_signal["wlp1s0"]) == [62.0, 62.0]
