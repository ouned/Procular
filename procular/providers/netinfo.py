"""Per-interface network details: addresses, link state, adapter, routes and Wi-Fi link quality.

Wi-Fi data comes from `iw` (nl80211); without it only the signal level from
/proc/net/wireless is available. The parsers are pure functions so they can be tested
with captured output.
"""

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import psutil

SYS_NET = "/sys/class/net"


def _read_str(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path):
    text = _read_str(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Wi-Fi helpers


def signal_quality(dbm):
    """Signal level in dBm -> 0..100 % (-100 dBm = 0 %, -50 dBm and better = 100 %)."""
    if dbm is None:
        return None
    return max(0.0, min(100.0, 2.0 * (dbm + 100)))


def signal_rating(dbm):
    if dbm is None:
        return None
    for limit, text in ((-50, "excellent"), (-60, "good"), (-70, "fair"), (-80, "weak")):
        if dbm >= limit:
            return text
    return "very weak"


def wifi_band(freq):
    """Frequency in MHz -> "2.4 GHz" / "5 GHz" / "6 GHz"."""
    if not freq:
        return None
    if freq < 3000:
        return "2.4 GHz"
    if freq < 5925:
        return "5 GHz"
    if freq < 7200:
        return "6 GHz"
    return "60 GHz"


def wifi_channel(freq):
    """Channel number for a 2.4/5/6 GHz center frequency (MHz)."""
    if not freq:
        return None
    freq = int(freq)
    if freq == 2484:
        return 14
    if 2412 <= freq < 2484:
        return (freq - 2407) // 5
    if 5000 <= freq < 5925:
        return (freq - 5000) // 5
    if 5950 <= freq <= 7125:
        return (freq - 5950) // 5
    return None


def wifi_standard(bitrates, freq=None):
    """Wi-Fi generation from iw bitrate strings ("1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 ...")."""
    text = " ".join(b for b in bitrates if b)
    if not text:
        return None
    if "EHT-MCS" in text:
        return "Wi-Fi 7 (802.11be)"
    if "HE-MCS" in text:
        return "Wi-Fi 6E (802.11ax)" if freq and freq >= 5925 else "Wi-Fi 6 (802.11ax)"
    if "VHT-MCS" in text:
        return "Wi-Fi 5 (802.11ac)"
    if re.search(r"\bMCS \d+", text):
        return "Wi-Fi 4 (802.11n)"
    if freq:
        return "802.11g" if freq < 3000 else "802.11a"
    return None


_RATE_RE = re.compile(r"([\d.]+)\s*MBit/s", re.I)
_MCS_RE = re.compile(r"\b(?:(?:EHT|HE|VHT)-)?MCS (\d+)")
_NSS_RE = re.compile(r"\b(?:EHT|HE|VHT)-NSS (\d+)")
_WIDTH_RE = re.compile(r"\b(\d+)MHz")


def parse_bitrate(text):
    """iw bitrate string -> {"rate": Mbit/s, "mcs": int, "nss": int, "width": MHz} (missing -> None)."""
    if not text:
        return None
    out = {"rate": None, "mcs": None, "nss": None, "width": None}
    m = _RATE_RE.search(text)
    if m:
        out["rate"] = float(m.group(1))
    for key, rx in (("mcs", _MCS_RE), ("nss", _NSS_RE), ("width", _WIDTH_RE)):
        m = rx.search(text)
        if m:
            out[key] = int(m.group(1))
    if out["mcs"] is not None and out["nss"] is None and "VHT" not in text and "HE" not in text:
        # 802.11n encodes the stream count in the MCS index (0-7: 1 stream, 8-15: 2, ...)
        out["nss"] = out["mcs"] // 8 + 1
    return out


def fmt_bitrate(info):
    if not info or info["rate"] is None:
        return "—"
    rate = info["rate"]
    text = f"{rate:.0f} Mbit/s" if rate >= 100 else f"{rate:g} Mbit/s"
    extra = []
    if info["mcs"] is not None:
        extra.append(f"MCS {info['mcs']}")
    if info["nss"]:
        extra.append(f"{info['nss']} stream{'s' if info['nss'] > 1 else ''}")
    if extra:
        text += f" ({', '.join(extra)})"
    return text


def _kv_lines(text):
    """'key: value' lines of iw output (indentation stripped, first colon splits)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            out.setdefault(key.strip().lower(), value.strip())
    return out


def _first_int(text):
    m = re.search(r"-?\d+", text or "")
    return int(m.group(0)) if m else None


def _first_float(text):
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def parse_iw_info(text):
    """`iw dev X info` -> ssid, freq, channel, width, center freq, tx power, interface type."""
    out = {"ssid": None, "freq": None, "channel": None, "width": None, "txpower": None, "mode": None}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ssid "):
            out["ssid"] = line[5:]
        elif line.startswith("type "):
            out["mode"] = line[5:]
        elif line.startswith("txpower "):
            out["txpower"] = _first_float(line[8:])
        elif line.startswith("channel "):
            m = re.match(r"channel (\d+) \((\d+(?:\.\d+)?) MHz\)(?:, width: (\d+) MHz)?", line)
            if m:
                out["channel"] = int(m.group(1))
                out["freq"] = float(m.group(2))
                out["width"] = int(m.group(3)) if m.group(3) else None
    return out


def parse_iw_station(text):
    """`iw dev X station dump` (managed mode: the access point) -> link statistics."""
    first = text.split("\nStation ", 1)[0]  # only the first station (the AP we are associated with)
    m = re.match(r"\s*Station ([0-9a-fA-F:]{17})", first)
    if not m:
        return None
    kv = _kv_lines(first)
    return {
        "bssid": m.group(1).lower(),
        "signal": _first_int(kv.get("signal")),
        "signal_avg": _first_int(kv.get("signal avg")),
        "beacon_signal": _first_int(kv.get("beacon signal avg")),
        "rx_bitrate": kv.get("rx bitrate"),
        "tx_bitrate": kv.get("tx bitrate"),
        "tx_retries": _first_int(kv.get("tx retries")),
        "tx_failed": _first_int(kv.get("tx failed")),
        "beacon_loss": _first_int(kv.get("beacon loss")),
        "connected_time": _first_int(kv.get("connected time")),
        "expected_throughput": _first_float(kv.get("expected throughput")),
        "inactive_ms": _first_int(kv.get("inactive time")),
    }


def parse_iw_link(text):
    """`iw dev X link` -> {"bssid", "ssid", "freq", "signal", "rx_bitrate", "tx_bitrate"} or None."""
    m = re.match(r"\s*Connected to ([0-9a-fA-F:]{17})", text)
    if not m:
        return None
    kv = _kv_lines(text)
    return {
        "bssid": m.group(1).lower(),
        "ssid": kv.get("ssid"),
        "freq": _first_float(kv.get("freq")),
        "signal": _first_int(kv.get("signal")),
        "rx_bitrate": kv.get("rx bitrate"),
        "tx_bitrate": kv.get("tx bitrate"),
    }


def parse_proc_wireless(text):
    """/proc/net/wireless -> {iface: {"quality": link, "signal": dBm}}."""
    out = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            link = float(fields[1].rstrip("."))
            level = float(fields[2].rstrip("."))
        except ValueError:
            continue
        out[name.strip()] = {"quality": link, "signal": int(level) if level < 0 else None}
    return out


# ---------------------------------------------------------------------------
# routes / DNS


def parse_proc_route(text, byteorder=sys.byteorder):
    """/proc/net/route -> {iface: (gateway, metric)} for IPv4 default routes.

    Addresses are 32-bit numbers in host byte order; `byteorder` is that of the machine that
    produced the text.
    """
    out = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000" or f[7] != "00000000":
            continue
        try:
            flags = int(f[3], 16)
            gw = socket.inet_ntoa(int(f[2], 16).to_bytes(4, byteorder))
            metric = int(f[6])
        except ValueError:
            continue
        if flags & 0x1 and flags & 0x2:  # RTF_UP | RTF_GATEWAY
            if f[0] not in out or metric < out[f[0]][1]:
                out[f[0]] = (gw, metric)
    return out


def parse_ipv6_route(text):
    """/proc/net/ipv6_route -> {iface: (gateway, metric)} for IPv6 default routes."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 10 or f[0] != "0" * 32 or f[1] != "00" or f[4] == "0" * 32:
            continue
        try:
            gw = str(ipaddress.IPv6Address(bytes.fromhex(f[4])))
            metric = int(f[5], 16)
        except ValueError:
            continue
        if f[9] not in out or metric < out[f[9]][1]:
            out[f[9]] = (gw, metric)
    return out


def parse_resolv_conf(text):
    """-> (nameservers, search domains)."""
    servers, search = [], []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
        elif parts and parts[0] in ("search", "domain"):
            search = parts[1:]
    return servers, search


def dns_servers():
    """Upstream DNS servers. systemd-resolved's /etc/resolv.conf only lists its 127.0.0.53
    stub, the real servers are in /run/systemd/resolve/resolv.conf."""
    result = ([], [])
    for path in ("/etc/resolv.conf", "/run/systemd/resolve/resolv.conf"):
        try:
            with open(path) as fh:
                servers, search = parse_resolv_conf(fh.read())
        except OSError:
            continue
        if servers and not all(s.startswith("127.0.0.5") for s in servers):
            return servers, search
        result = (result[0] or servers, result[1] or search)
    return result


# ---------------------------------------------------------------------------
# adapter / addresses


def iface_kind(name, sys_net=SYS_NET):
    base = os.path.join(sys_net, name)
    if os.path.isdir(os.path.join(base, "wireless")) or os.path.exists(os.path.join(base, "phy80211")):
        return "Wi-Fi"
    devtype = None
    uevent = _read_str(os.path.join(base, "uevent")) or ""
    for line in uevent.splitlines():
        if line.startswith("DEVTYPE="):
            devtype = line[8:]
    kinds = {"bridge": "Bridge", "vlan": "VLAN", "wireguard": "WireGuard", "bond": "Bond",
             "wwan": "Mobile broadband", "gadget": "USB gadget", "vxlan": "VXLAN", "macvlan": "MACVLAN"}
    if devtype in kinds:
        return kinds[devtype]
    if os.path.isdir(os.path.join(base, "bridge")):
        return "Bridge"
    if os.path.isdir(os.path.join(base, "bonding")):
        return "Bond"
    if os.path.exists(os.path.join(base, "tun_flags")):
        return "TUN/TAP"
    if not os.path.exists(os.path.join(base, "device")):
        if name.startswith("veth"):
            return "Virtual Ethernet"
        return "Virtual"
    arptype = _read_int(os.path.join(base, "type"))
    if arptype == 1:
        return "Ethernet"
    if arptype == 512:
        return "PPP"
    if arptype == 65534:
        return "Tunnel"
    return "Network"


def adapter_name(name, sys_net=SYS_NET):
    """Marketing name of the hardware behind an interface (PCI via pci.ids, USB via sysfs)."""
    dev = os.path.realpath(os.path.join(sys_net, name, "device"))
    if not os.path.exists(dev):
        return None
    subsystem = os.path.basename(os.path.realpath(os.path.join(dev, "subsystem")))
    if subsystem == "pci":
        from .pci import pci_device_name

        try:
            vendor = int(_read_str(os.path.join(dev, "vendor")), 16)
            device = int(_read_str(os.path.join(dev, "device")), 16)
        except (TypeError, ValueError):
            return None
        return pci_device_name(vendor, device) or f"PCI {vendor:04x}:{device:04x}"
    if subsystem == "usb":  # dev is the USB interface, the device with the names is its parent
        parent = os.path.dirname(dev)
        product = _read_str(os.path.join(parent, "product"))
        maker = _read_str(os.path.join(parent, "manufacturer"))
        if product:
            return f"{maker} {product}" if maker and maker not in product else product
    return None


def _prefixlen(netmask):
    if not netmask:
        return None
    try:
        return ipaddress.ip_network(f"0.0.0.0/{netmask}").prefixlen if "." in netmask \
            else bin(int(ipaddress.IPv6Address(netmask))).count("1")
    except ValueError:
        return None


_ULA = ipaddress.IPv6Network("fc00::/7")


def split_addresses(addrs):
    """psutil snicaddr list -> (["192.168.1.2/24"], ["2001:db8::1/64", "fe80::1/64"]) (global v6 first)."""
    v4, v6 = [], []
    for a in addrs:
        if a.family == socket.AF_INET:
            plen = _prefixlen(a.netmask)
            v4.append(f"{a.address}/{plen}" if plen is not None else a.address)
        elif a.family == socket.AF_INET6:
            addr = a.address.split("%", 1)[0]
            plen = _prefixlen(a.netmask)
            v6.append(f"{addr}/{plen}" if plen is not None else addr)

    def v6_key(text):
        try:
            ip = ipaddress.IPv6Address(text.split("/", 1)[0])
        except ValueError:
            return 3
        return 2 if ip.is_link_local else 1 if ip in _ULA else 0

    v6.sort(key=v6_key)
    return v4, v6


# ---------------------------------------------------------------------------


def _find_iw():
    return shutil.which("iw") or next((p for p in ("/usr/sbin/iw", "/sbin/iw") if os.access(p, os.X_OK)), None)


class NetInfoReader:
    """Collects network details on every sampler tick (runs in the sampler thread)."""

    STATIC_TTL = 30.0  # adapter names, kinds and drivers rarely change; re-check now and then

    def __init__(self, skip_prefixes=("lo",), sys_net=SYS_NET):
        self.skip = skip_prefixes
        self.sys_net = sys_net
        self.iw = _find_iw()
        self._static = {}  # name -> (time, dict)
        self._iw_ok = True

    def _static_info(self, name, now):
        cached = self._static.get(name)
        if cached and now - cached[0] < self.STATIC_TTL:
            return cached[1]
        base = os.path.join(self.sys_net, name)
        driver = os.path.realpath(os.path.join(base, "device", "driver"))
        info = {
            "kind": iface_kind(name, self.sys_net),
            "adapter": adapter_name(name, self.sys_net),
            "driver": os.path.basename(driver) if os.path.exists(driver) else None,
        }
        self._static[name] = (now, info)
        return info

    def _run_iw(self, *args):
        try:
            res = subprocess.run([self.iw, *args], capture_output=True, text=True, timeout=1)
        except (OSError, subprocess.SubprocessError):
            return None
        return res.stdout if res.returncode == 0 else None

    def wifi(self, name, proc_wireless):
        """Wi-Fi link details; "connected" is False when not associated."""
        out = {"connected": False, "ssid": None, "bssid": None, "freq": None, "channel": None, "width": None,
               "band": None, "signal": None, "signal_avg": None, "quality": None, "rating": None,
               "rx_bitrate": None, "tx_bitrate": None, "standard": None, "txpower": None,
               "tx_retries": None, "tx_failed": None, "beacon_loss": None, "connected_time": None,
               "expected_throughput": None, "mode": None}
        if self.iw and self._iw_ok:
            info_text = self._run_iw("dev", name, "info")
            if info_text is None and self._run_iw("--version") is None:
                self._iw_ok = False  # unusable binary, don't keep spawning it
            if info_text:
                out.update(parse_iw_info(info_text))
                sta = parse_iw_station(self._run_iw("dev", name, "station", "dump") or "")
                if sta and out["mode"] in (None, "managed"):
                    out.update({k: v for k, v in sta.items() if k not in ("rx_bitrate", "tx_bitrate")})
                    out["rx_bitrate"] = parse_bitrate(sta["rx_bitrate"])
                    out["tx_bitrate"] = parse_bitrate(sta["tx_bitrate"])
                    out["standard"] = wifi_standard([sta["rx_bitrate"], sta["tx_bitrate"]], out["freq"])
                    out["connected"] = True
                elif out["ssid"]:  # AP / ad-hoc mode, or the station dump was not readable
                    out["connected"] = True
        if out["signal"] is None and name in proc_wireless:
            out["signal"] = proc_wireless[name]["signal"]
            out["connected"] = out["connected"] or out["signal"] is not None
        if out["freq"] and out["channel"] is None:
            out["channel"] = wifi_channel(out["freq"])
        out["band"] = wifi_band(out["freq"])
        out["quality"] = signal_quality(out["signal"])
        out["rating"] = signal_rating(out["signal"])
        return out

    def wifi_quality(self, path="/proc/net/wireless"):
        """-> {wireless interface: signal quality % (0 when not connected)}. Cheap: one small file."""
        try:
            with open(path) as fh:
                wireless = parse_proc_wireless(fh.read())
        except OSError:
            return {}
        return {name: signal_quality(w["signal"]) or 0 for name, w in wireless.items()}

    def read(self, counters=None):
        """-> {"ifaces": {name: details}, "dns": [...], "search": [...], "hostname": str, "default": name}."""
        now = time.monotonic()
        try:
            stats = psutil.net_if_stats()
        except OSError:
            stats = {}
        try:
            addrs = psutil.net_if_addrs()
        except OSError:
            addrs = {}
        routes4 = routes6 = {}
        try:
            with open("/proc/net/route") as fh:
                routes4 = parse_proc_route(fh.read())
        except OSError:
            pass
        try:
            with open("/proc/net/ipv6_route") as fh:
                routes6 = parse_ipv6_route(fh.read())
        except OSError:
            pass
        proc_wireless = {}
        try:
            with open("/proc/net/wireless") as fh:
                proc_wireless = parse_proc_wireless(fh.read())
        except OSError:
            pass

        ifaces = {}
        for name, st in stats.items():
            if name.startswith(self.skip):
                continue
            base = os.path.join(self.sys_net, name)
            info = dict(self._static_info(name, now))
            v4, v6 = split_addresses(addrs.get(name, []))
            mac = next((a.address for a in addrs.get(name, []) if a.family == psutil.AF_LINK), None)
            info.update({
                "up": st.isup,
                "operstate": _read_str(os.path.join(base, "operstate")),
                "carrier": _read_int(os.path.join(base, "carrier")) == 1 if st.isup else False,
                "speed": st.speed or None,  # Mbit/s
                "duplex": {psutil.NIC_DUPLEX_FULL: "full", psutil.NIC_DUPLEX_HALF: "half"}.get(st.duplex),
                "mtu": st.mtu,
                "mac": mac if mac and mac != "00:00:00:00:00:00" else None,
                "ipv4": v4,
                "ipv6": v6,
                "gateway4": routes4.get(name, (None,))[0],
                "gateway6": routes6.get(name, (None,))[0],
                "wifi": None,
            })
            c = (counters or {}).get(name)
            if c is not None:
                info.update({"rx_packets": c.packets_recv, "tx_packets": c.packets_sent,
                             "rx_errors": c.errin, "tx_errors": c.errout,
                             "rx_dropped": c.dropin, "tx_dropped": c.dropout})
            if info["kind"] == "Wi-Fi" and st.isup:
                info["wifi"] = self.wifi(name, proc_wireless)
            ifaces[name] = info
        for name in list(self._static):
            if name not in stats:
                del self._static[name]

        candidates = [(m, n) for n, (_gw, m) in routes4.items()] or [(m, n) for n, (_gw, m) in routes6.items()]
        servers, search = dns_servers()
        return {
            "ifaces": ifaces,
            "default": min(candidates)[1] if candidates else None,
            "dns": servers,
            "search": search,
            "hostname": socket.gethostname(),
        }
