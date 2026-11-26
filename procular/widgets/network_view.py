"""Network tab: TCP/UDP sockets with owning process."""

import ipaddress
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..providers.network import TrafficRates, list_connections, stamp_owners, tcp_traffic, valid_owners
from ..providers.processes import username
from ..util import privileged
from ..util.format import fmt_bit_rate, fmt_bytes
from ..util.icons import GENERIC_ICON
from ..util.tasks import run_async
from . import process_actions as pa
from .info_menu import SOCKET_DOTS, legend_item
from .table import Column, Table


# TCP state -> status-dot class (see style.css)
_STATE_CLASS = {"Established": "running", "Connected": "running", "Listen": "active"}
_STATE_CLASS.update(dict.fromkeys(("SYN sent", "SYN received", "New SYN received", "FIN wait 1", "FIN wait 2",
                                   "Time wait", "Close wait", "Last ACK", "Closing"), "changing"))
# stand-ins for a process we may not see
_NO_OWNER = ("Unknown", "Waiting connection")


def _owner_markup(name):
    text = GLib.markup_escape_text(name or "")
    return f"<span alpha='45%'>{text}</span>" if name in _NO_OWNER else text


def _load(owners):
    """The connections, the owners found as administrator that still hold, and the TCP byte
    counters. Runs in a worker."""
    valid = valid_owners(owners)
    return list_connections({inode: pid for inode, (pid, _start) in valid.items()}), valid, tcp_traffic()


def _state_class(data):
    return _STATE_CLASS.get(data.get("state"), "inactive")


def state_column():
    """The socket state as a dot only, explained by state_legend() or the info popover; its text still
    filters, sorts and copies."""
    return Column("state", "", "state", width=30, markup=lambda _v: "", indicator=_state_class,
                  tooltip=lambda d: d.get("state") or "Not connected")


def state_legend():
    """A row explaining the state dots, for below a socket table."""
    legend = Gtk.Box(spacing=18, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
    for state, text in SOCKET_DOTS[1]:
        dot = Gtk.Box(css_classes=["status-dot", f"status-{state}"], valign=Gtk.Align.CENTER)
        legend.append(legend_item(dot, text))
    return legend


def traffic_columns(width=100):
    """Receive and send rate columns for socket rows that carry recv_rate/send_rate and the
    received/sent totals (see add_traffic)."""
    def total(key, verb):
        return lambda d: f"{fmt_bytes(d[key])} {verb} in total" if d.get(key) is not None else None

    return [Column("recv_rate", "Receive rate", "recv_rate", fmt=fmt_bit_rate, numeric=True, width=width,
                   tooltip=total("received", "received")),
            Column("send_rate", "Send rate", "send_rate", fmt=fmt_bit_rate, numeric=True, width=width,
                   tooltip=total("sent", "sent"))]


def add_traffic(row, traffic, rates):
    """Put a socket's byte totals and rates into its row: None where the kernel keeps none (UDP)."""
    sent, received = traffic.get(row["inode"], (None, None))
    send_rate, recv_rate = rates.get(row["inode"], (None, None))
    row.update(sent=sent, received=received, send_rate=send_rate, recv_rate=recv_rate)


def traffic_summary(rows):
    """" · ↓ X/s · ↑ Y/s": received and sent per second, summed over the rows that have rates
    ("" when none has)."""
    rated = [r for r in rows if r.get("recv_rate") is not None]
    if not rated:
        return ""
    recv = sum(r["recv_rate"] for r in rated)
    send = sum(r["send_rate"] for r in rated)
    return f" · ↓ {fmt_bit_rate(recv) or '0 bit/s'} · ↑ {fmt_bit_rate(send) or '0 bit/s'}"


def _is_public(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast)


def _format_endpoint(addr, port):
    """Format an IPv4/IPv6 address and optional port for copying."""
    if not port:
        return addr
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def _remote(d):
    """The remote address, or None for a socket without a peer (listening, unconnected UDP)."""
    addr = d.get("raddr")
    if not addr or (addr in ("0.0.0.0", "::") and not d.get("rport")):
        return None
    return addr


def _local(d):
    return _format_endpoint(d["laddr"], d.get("lport")) if d.get("laddr") else None


def _peer(d):
    addr = _remote(d)
    return _format_endpoint(addr, d.get("rport")) if addr else None


def _text(value):
    return None if value in (None, "", 0) else str(value)


# Copy submenu of a socket table: (action, label, value(row) -> text, or None where it does not apply)
COPY_FIELDS = [
    ("copy-local", "Local Address", _local),
    ("copy-local-ip", "Local IP", lambda d: d.get("laddr") or None),
    ("copy-local-port", "Local Port", lambda d: _text(d.get("lport"))),
    ("copy-remote", "Remote Address", _peer),
    ("copy-remote-ip", "Remote IP", _remote),
    ("copy-remote-port", "Remote Port", lambda d: _text(d.get("rport")) if _remote(d) else None),
]
# only in the main window's list, which knows owners and host names
OWNER_COPY_FIELDS = [
    ("copy-host", "Remote Host", lambda d: d.get("host") or None),
    ("copy-process", "Process Name", lambda d: d.get("process") if d.get("pid") else None),
    ("copy-pid", "PID", lambda d: _text(d.get("pid"))),
]


NETWORK_FILTER_FIELDS = ("process", "pid", "proto", "state", _local, _peer, "host", "user", "unit",
                         lambda d: fmt_bit_rate(d.get("recv_rate")), lambda d: fmt_bit_rate(d.get("send_rate")))


def copy_submenu(prefix, fields):
    """The Copy submenu for `fields`, grouped as local / remote / owner, with the whole row last."""
    sub = Gio.Menu()
    for i in range(0, len(fields), 3):
        group = Gio.Menu()
        for name, label, _value in fields[i:i + 3]:
            group.append(label, f"{prefix}.{name}")
        sub.append_section(None, group)
    row = Gio.Menu()
    row.append("Row", f"{prefix}.copy")
    sub.append_section(None, row)
    return sub


def add_copy_actions(group, table, fields):
    """Actions for copy_submenu(); an entry is greyed out where the selected socket has no such value."""
    actions = []
    for name, _label, value in fields:
        action = Gio.SimpleAction.new(name, None)

        def activate(*_a, value=value):
            data = table.selected()
            text = value(data) if data else None
            if text:
                pa.copy_text(text)

        action.connect("activate", activate)
        group.add_action(action)
        actions.append((action, value))
    row = Gio.SimpleAction.new("copy", None)
    row.connect("activate", lambda *_: table.copy_selected())
    group.add_action(row)

    def sync(*_):
        data = table.selected()
        for action, value in actions:
            action.set_enabled(bool(data and value(data)))

    table.connect("selection-changed", sync)
    if table.popover is not None:
        table.popover.connect("show", sync)  # the row may have changed since it was selected
    sync()


MAX_DNS_CACHE = 4096
DNS_CACHE_TTL = 600.0  # seconds


class NetworkView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self._active = False
        self._loading = False
        self._refresh_again = False
        self._timer = 0
        # inode -> (pid, start time): socket owners found as administrator. Replaced, never
        # changed in place: a worker reads it.
        self._extra_owners = {}
        self._resolved = False  # asked as administrator before: what is still unknown has no process
        self._dns = {}  # addr -> (host, monotonic timestamp)
        self._dns_pending = set()
        self._traffic = TrafficRates()
        self.resolver = Gio.Resolver.get_default()

        self.banner = Adw.Banner(button_label="Resolve as Administrator")
        self.banner.connect("button-clicked", self._resolve_elevated)
        self.append(self.banner)

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        config = win.config
        self.show_listen = Gtk.CheckButton(label="Show listening sockets",
                                           active=config.get("network_show_listening"))
        self.show_listen.connect("toggled", self._on_option_toggled, "network_show_listening")
        self.resolve_dns = Gtk.CheckButton(label="Resolve host names", active=config.get("network_resolve_dns"))
        self.resolve_dns.connect("toggled", self._on_option_toggled, "network_resolve_dns")
        bar.append(self.show_listen)
        bar.append(self.resolve_dns)
        bar.append(Gtk.Box(hexpand=True))
        self.count = Gtk.Label(css_classes=["dim"])
        bar.append(self.count)
        self.append(bar)

        columns = [
            state_column(),
            Column("process", "Process", "process", width=170, icon=self._icon, markup=_owner_markup),
            Column("pid", "PID", "pid", numeric=True, width=65),
            Column("proto", "Protocol", "proto", width=65),
            *traffic_columns(width=95),  # before the wide address columns: visible without scrolling
            # wide enough for most IPv6 addresses; longer ones keep both ends and show in full on hover
            Column("laddr", "Local address", "laddr", width=190, ellipsize=Pango.EllipsizeMode.MIDDLE,
                   tooltip=lambda d: d["laddr"] or None),
            Column("lport", "Local port", "lport", numeric=True, width=75),
            Column("raddr", "Remote address", "raddr", width=190, ellipsize=Pango.EllipsizeMode.MIDDLE,
                   tooltip=lambda d: d["raddr"] or None),
            Column("rport", "Remote port", "rport", numeric=True, width=85),
            Column("host", "Remote host", "host", width=130, expand=True),
            Column("user", "User", "user", width=80),
            Column("unit", "systemd unit", "unit", width=160, visible=False),
        ]
        self.table = Table(columns, key="key", filter_keys=NETWORK_FILTER_FIELDS, menu_model=self._build_menu())
        self.table.remember_sort(config, "network")
        self.table.connect("activated", lambda *_: self._goto())
        self.append(self.table)  # the state dots are explained in the info popover (SOCKET_DOTS)
        self._install_actions()

    def _icon(self, data):
        item = self.win.tree.get(data["pid"]) if data.get("pid") else None
        return item.icon if item is not None and item.icon is not None else GENERIC_ICON

    def _build_menu(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("Go to Process", "net.goto")
        s1.append("Process Properties", "net.properties")
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append("Close Connection", "net.close")
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append_submenu("Copy", copy_submenu("net", COPY_FIELDS + OWNER_COPY_FIELDS))
        s3.append("Whois Remote Address", "net.whois")
        menu.append_section(None, s3)
        return menu

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        for name, handler in (("goto", self._goto), ("properties", self._properties), ("close", self._close),
                              ("whois", self._whois)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, h=handler: h())
            group.add_action(a)
        add_copy_actions(group, self.table, COPY_FIELDS + OWNER_COPY_FIELDS)
        self.insert_action_group("net", group)

    def _on_option_toggled(self, button, key):
        self.win.config.set(key, button.get_active())
        self.refresh(force=True)

    # ------------------------------------------------------------------ polling
    def set_active(self, active):
        if active == self._active:
            return
        self._active = active
        if active:
            self.refresh(force=True)
            if not self._timer:
                self._timer = GLib.timeout_add_seconds(2, self._on_timer)
        elif self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if not active:
            self._traffic.reset()

    def _on_timer(self):
        if self._active and not self.win.sampler.paused:
            self.refresh()
        return True

    def refresh(self, force=False):
        if self._loading:
            if force:
                self._refresh_again = True
            return
        self._loading = True
        owners = self._extra_owners
        run_async(_load, lambda result, error: self._on_conns(result, error, owners), owners)

    def _on_conns(self, result, error, asked):
        self._loading = False
        again, self._refresh_again = self._refresh_again, False
        if not self._active:
            return
        if again:
            self.refresh()
        if error or result is None:
            return
        conns, valid, traffic = result
        rates = self._traffic.update(traffic)
        if self._extra_owners is asked:  # else: resolved again meanwhile
            # Forget owners whose process is gone and sockets that are closed: both numbers are
            # used again, and an old entry would then name the wrong process.
            inodes = {c["inode"] for c in conns}
            self._extra_owners = {inode: owner for inode, owner in valid.items() if inode in inodes}
        show_listen = self.show_listen.get_active()
        resolve = self.resolve_dns.get_active()
        rows = []
        unknown = 0
        for c in conns:
            if not show_listen and c["state"] == "Listen":
                continue
            item = self.win.tree.get(c["pid"]) if c["pid"] else None
            if c["pid"] is None and c["state"] != "Time wait":
                unknown += 1
            row = dict(c)
            row["process"] = item.name if item else ("Unknown" if c["state"] != "Time wait" else "Waiting connection")
            row["unit"] = item.rec.get("unit") if item else ""
            row["user"] = username(c["uid"])
            row["raddr"] = "" if c["raddr"] in ("0.0.0.0", "::") and c["rport"] == 0 else c["raddr"]
            row["rport"] = c["rport"] or None
            row["lport"] = c["lport"]
            host = ""
            if resolve and row["raddr"] and _is_public(row["raddr"]):
                host = self._lookup(row["raddr"])
            row["host"] = host
            add_traffic(row, traffic, rates)
            rows.append(row)
        self.table.set_rows(rows, highlight=self.win.config.get("highlight_seconds") or None,
                            now=time.monotonic())
        self.count.set_text(f"{len(rows)} sockets{traffic_summary(rows)}")
        if unknown:
            if self._resolved:
                self.banner.set_title(f"{unknown} sockets without a known process: opened since the owners "
                                      "were resolved, or held by the kernel")
                self.banner.set_button_label("Resolve Again")
            else:
                self.banner.set_title(f"{unknown} sockets belong to processes of other users")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _store_dns(self, addr, host):
        if addr not in self._dns and len(self._dns) >= MAX_DNS_CACHE:
            self._dns.pop(next(iter(self._dns)))
        self._dns[addr] = (host, time.monotonic())

    def _lookup(self, addr):
        entry = self._dns.get(addr)
        if entry is not None:
            host, when = entry
            if time.monotonic() - when < DNS_CACHE_TTL:
                return host
            del self._dns[addr]
        if addr not in self._dns_pending and len(self._dns_pending) < 16:
            self._dns_pending.add(addr)
            inet = Gio.InetAddress.new_from_string(addr)
            if inet is None:
                self._dns_pending.discard(addr)
                self._store_dns(addr, "")
                return ""

            def done(resolver, res):
                self._dns_pending.discard(addr)
                try:
                    self._store_dns(addr, resolver.lookup_by_address_finish(res))
                except GLib.Error:
                    self._store_dns(addr, "")

            self.resolver.lookup_by_address_async(inet, None, done)
        return ""

    def _resolve_elevated(self, *_):
        def done(result, error):
            if error:
                self.win.toast(f"Failed: {error}")
                return
            self._extra_owners = stamp_owners(result or {})
            self._resolved = True
            if self._active:
                self.refresh(force=True)

        privileged.run(["sockets"], done)

    def set_filter_text(self, text):
        self.table.set_filter_text(text)

    # ------------------------------------------------------------------ actions
    def _goto(self):
        data = self.table.selected()
        if data and data.get("pid"):
            self.win.goto_process(data["pid"])

    def _properties(self):
        data = self.table.selected()
        if data and data.get("pid"):
            self.win.open_properties(data["pid"])

    def _close(self):
        data = self.table.selected()
        if not data:
            return
        if not data["proto"].startswith("tcp") or data["state"] in ("Listen", "Time wait") or not data.get("raddr"):
            self.win.toast("Only established TCP connections can be closed")
            return

        def done(closed, error):
            if error:
                self.win.toast(f"Failed: {error}")
            elif closed:
                self.win.toast("Connection closed")
            else:  # ss -K found nothing to close, or the kernel cannot do it (CONFIG_INET_DIAG_DESTROY)
                self.win.toast("The connection was not closed: it is gone already, or this kernel "
                               "cannot close sockets")
            self.refresh()

        privileged.run(["close-socket", data["laddr"], data["lport"], data["raddr"], data["rport"] or 0], done)

    def _whois(self):
        data = self.table.selected()
        if data and data.get("raddr"):
            Gtk.UriLauncher.new(f"https://who.is/whois-ip/ip-address/{data['raddr']}").launch(self.win, None, None)
