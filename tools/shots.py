#!/usr/bin/env python3
"""Run Procular from this source tree and render its windows to PNG (screenshots, visual checks).

Not part of the installed app: it starts the app itself and hooks in once the main window is up.
Run it in an off-screen session: tools/headless-shot.sh, tools/readme-shots.sh.

PROCULAR_SCREENSHOT=/path/out.png   render after PROCULAR_SHOT_DELAY seconds (default 4) and quit
PROCULAR_SHOT_PLAN=plan.json        instead: several screenshots from one run, see install_plan()
PROCULAR_SHOT_ACTION=...           comma separated: page:<name>, sysinfo, prefs, props:<pid>|@<name>,
                                    search:<text>, sort:<column>, propspage:<name>, sysinfopage:<name>,
                                    sysinfosize:<w>x<h> (resize the System Information window),
                                    propssize:<w>x<h> (resize the Properties window),
                                    propsexit (show the Properties window as if the process exited),
                                    devprops:<text> (open the Properties window of the first device of the
                                    Devices tab whose name or address contains text), devpropspage:<name>,
                                    propsscroll (scroll the visible Properties page to the end),
                                    graphhover:<x>[/<y>] (simulated pointer over the System Information graphs;
                                    graphhover:big:<x>[/<y>] not over the sidebar's, graphhover:off),
                                    sysinfocores:on|off (one graph per CPU),
                                    procmenu[:<pid>] (open the process context menu), procsub:<label> (open a submenu),
                                    colors (open the row colours popover), set:<key>=<json> (change a setting),
                                    info (open the explanations popover),
                                    find:<text>[|<kind>] (search in the Find Open Files window),
                                    about[:<seconds>] (open the About dialog without music; with seconds:
                                    the logo animation stands still at that moment)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

import procular  # noqa: E402, F401  (pins the Gtk 4 and Adw 1 typelibs)

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402


def render_widget(widget, path, quiet=False):
    width, height = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        if quiet:
            return False
        print("nothing to render", path, type(widget).__name__, f"{width}x{height}",
              "mapped" if widget.get_mapped() else "unmapped", flush=True)
        return False
    renderer = widget.get_native().get_renderer()
    rect = Graphene.Rect().init(0, 0, width, height)
    texture = renderer.render_texture(node, rect)
    ok = texture.save_to_png(path)
    if not ok:
        print('save_to_png failed', path)
    return ok


def focus_window(window, then, waited=0):
    """Give `window` the focus, so that it is not drawn dimmed (the "backdrop" look), then call then().

    The off-screen compositor focuses a window when it is mapped and keeps the focus there: a
    later present() is refused, as it carries no activation token (those need user input). So a
    window that lost the focus to one opened after it is mapped again."""
    surface = window.get_surface() if window is not None else None
    if surface is None or surface.get_state() & Gdk.ToplevelState.FOCUSED:
        then()
        return False
    if waited == 0:
        window.set_visible(False)
        window.set_visible(True)
    if waited >= 40:  # 2 s
        print("could not focus", window.get_title(), flush=True)
        then()
        return False
    GLib.timeout_add(50, focus_window, window, then, waited + 1)
    return False


_REDACT_CSS = None


def redact(titles):
    """Blur the values of the label/value rows called `titles` in every window (redact:<title>|...),
    and any other label that shows one of those values, such as a network name in a status line.
    For screenshots that are published: host names, addresses, network names."""
    global _REDACT_CSS
    if _REDACT_CSS is None:
        from gi.repository import Gdk

        _REDACT_CSS = Gtk.CssProvider()
        _REDACT_CSS.load_from_string(".procular-redacted { filter: blur(6px); }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), _REDACT_CSS,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    wanted = set(titles)
    labels = []

    def walk(widget):
        if isinstance(widget, Gtk.Label):
            labels.append(widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    for window in Gtk.Window.list_toplevels():
        walk(window)
    secrets = set()
    for label in labels:
        grid = label.get_parent()
        if label.get_text() in wanted and isinstance(grid, Gtk.Grid):
            col, row, _w, _h = grid.query_child(label)
            value = grid.get_child_at(col + 1, row)
            if isinstance(value, Gtk.Label):
                value.add_css_class("procular-redacted")
                secrets.update(line for line in value.get_text().split() if len(line) >= 4 and line != "—")
    for label in labels:
        text = label.get_text()
        if any(secret in text for secret in secrets) and text not in wanted:
            label.add_css_class("procular-redacted")


def apply_action(app, action):
    """Run one PROCULAR_SHOT_ACTION step (see the module docstring)."""
    win = app.window
    kind, _, arg = action.partition(":")
    if kind == "page":
        win.stack.set_visible_child_name(arg)
    elif kind == "sysinfo":
        app.activate_action("sysinfo", None)
    elif kind == "prefs":
        app.activate_action("preferences", None)
    elif kind == "props":
        if arg.startswith("@"):  # by name: the oldest process called that
            named = [i for i in app.tree.items.values() if i.rec.get("name") == arg[1:]]
            if named:
                win.open_properties(min(named, key=lambda i: i.rec.get("create_time") or 0))
        else:
            win.open_properties(int(arg) if arg else os.getpid())
    elif kind == "search":
        win.search_bar.set_search_mode(True)
        win.search_entry.set_text(arg)
    elif kind == "sort":
        col = win.process_view._columns[arg]
        win.process_view.view.sort_by_column(col, Gtk.SortType.DESCENDING)
    elif kind == "propspage":
        for pw in win._properties.values():
            pw.stack.set_visible_child_name(arg)
    elif kind == "propssize":
        w, _, h = arg.partition("x")
        for pw in win._properties.values():
            pw.set_default_size(int(w), int(h))
    elif kind == "propsscroll":
        # scroll the visible properties page to the end (for checking long pages)
        for pw in win._properties.values():
            page = pw.stack.get_visible_child()
            if isinstance(page, Gtk.ScrolledWindow):
                adj = page.get_vadjustment()
                GLib.timeout_add(300, lambda a=adj: a.set_value(a.get_upper()) and False)
    elif kind == "devprops":
        rows = win.devices_view.table._rows
        text = arg.casefold()
        for key, row in rows.items():
            if key != "/" and (text in row.data["name"].casefold() or text in row.data["address"].casefold()):
                win.open_device_properties(key)
                break
    elif kind == "devpropspage":
        for dw in win._device_properties.values():
            dw.stack.set_visible_child_name(arg)
    elif kind == "propsexit":
        for pw in win._properties.values():
            pw._mark_exited()
    elif kind == "sysinfopage" and app.sysinfo_window is not None:
        app.sysinfo_window.stack.set_visible_child_name(arg)
    elif kind == "sysinfosize" and app.sysinfo_window is not None:
        w, _, h = arg.partition("x")
        app.sysinfo_window.set_size_request(int(w), int(h))
    elif kind == "graphhover" and app.sysinfo_window is not None:
        # simulate the pointer at x[/y] (graph pixels) over every System Information graph
        # "off" hides it again; "big:x[/y]" only over the full-size graphs (not the sidebar's)
        big = arg.startswith("big:")
        x, _, y = arg.removeprefix("big:").partition("/")
        for graph in app.sysinfo_window.graphs:
            show = arg != "off" and not (big and graph.compact)
            graph._hover = (float(x), float(y or 40)) if show else None
            graph.queue_draw()
    elif kind == "sysinfocores" and app.sysinfo_window is not None:
        app.sysinfo_window.per_core.set_active(arg == "on")
    elif kind == "redact":
        redact(arg.split("|"))
        GLib.timeout_add(1000, lambda: redact(arg.split("|")) and False)  # values that arrived since
    elif kind == "procmenu":
        # no transitions: the shot would catch a submenu half slid in
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        view = win.process_view
        item = win.tree.get(int(arg) if arg else os.getpid())
        if item is not None:
            view.select_pid(item.pid)
            view.popup_menu(item, 40, 0)
    elif kind == "colors":
        win.colors_button.popup()
    elif kind == "info":
        win.info_button.popup()
    elif kind == "about":
        app.config.data["about_music"] = False  # nobody listens to a screenshot
        app.activate_action("about", None)
        if arg:
            win.get_visible_dialog().logo.freeze(float(arg))
    elif kind == "set":
        import json
        key, _, value = arg.partition("=")
        app.config.set(key, json.loads(value))
    elif kind == "procsub":
        win.process_view.popover.set_property("visible-submenu", arg)
    elif kind == "find":  # "find:QUERY" or "find:QUERY|KIND" (a kind id of find_files.KIND_CHOICES)
        text, _, find_kind = arg.partition("|")
        app.show_find_files(text, find_kind or None)


def install(app):
    path = os.environ.get("PROCULAR_SCREENSHOT")
    if not path:
        return
    if os.environ.get("PROCULAR_SHOT_PLAN"):
        install_plan(app, path, os.environ["PROCULAR_SHOT_PLAN"])
        return
    delay = float(os.environ.get("PROCULAR_SHOT_DELAY", "4"))
    actions = [a for a in os.environ.get("PROCULAR_SHOT_ACTION", "").split(",") if a]

    def run_actions():
        for action in actions:
            apply_action(app, action)
        return False

    def shoot(attempt=0, focused=False):
        target = None
        dialog = None
        for w in Gtk.Window.list_toplevels():
            if w.get_visible() and w is not app.window and isinstance(w, Gtk.Window) and w.get_title():
                target = w
        target = target or app.window
        # libadwaita dialogs live inside the parent window
        visible = getattr(target, "get_visible_dialog", None)
        dialog = visible() if visible else None
        menu = getattr(getattr(app.window, "process_view", None), "popover", None)
        if menu is not None and menu.get_visible():
            target = menu  # popovers are their own surface; render just the menu
        for name in ("colors_button", "info_button"):
            popover = getattr(getattr(app.window, name, None), "popover", None)
            if popover is not None and popover.get_visible():
                target = popover
        if isinstance(target, Gtk.Window) and not focused:
            focus_window(target, lambda: shoot(attempt, True))
            return False
        if not render_widget(target, path, quiet=attempt < 20):
            if attempt < 20:  # nothing to render until the window has drawn itself again
                target.queue_draw()
                GLib.timeout_add(250, shoot, attempt + 1, True)
                return False
        title = target.get_title() if isinstance(target, Gtk.Window) else type(target).__name__
        print("screenshot", path, title, "dialog" if dialog else "")
        app.quit()
        return False

    GLib.timeout_add(int(delay * 500), run_actions)
    GLib.timeout_add(int(delay * 1000), shoot)


def install_plan(app, out_dir, plan_path):
    """PROCULAR_SHOT_PLAN=plan.json: several screenshots from one run, into the directory
    PROCULAR_SCREENSHOT, after a warm-up that fills the graphs (tools/readme-shots.sh).

    plan: {"warmup": seconds, "settle": seconds after a shot's actions,
           "sizes": {"main" | "sysinfo" | "props" | "devprops": "<w>x<h>"},
           "early": [actions run at start, e.g. "props:@name" to collect a process's history],
           "shots": [{"name": ..., "target": "main" | "sysinfo" | "props", "actions": [...],
                      "wait": {"cpu": percent, "disk": bytes/s, "samples": n}: first wait (up to
                               30 s) until the system has been at least that busy for n updates}]}
    Each shot is saved as <name>-<PROCULAR_SHOT_THEME>.png in that colour scheme ("light" or
    "dark"; one run per theme, so that each one starts with its own styles).
    """
    import json

    with open(plan_path) as fh:
        plan = json.load(fh)
    theme = os.environ.get("PROCULAR_SHOT_THEME", "light")
    os.makedirs(out_dir, exist_ok=True)
    queue = list(plan["shots"])
    settle = int(plan.get("settle", 1.5) * 1000)
    app.config.set("color_scheme", theme)
    # no transitions: a shot right after a page switch would catch it half done
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

    def target_of(kind):
        if kind == "sysinfo":
            return app.sysinfo_window
        if kind == "props":
            return next(iter(app.window._properties.values()), None)
        if kind == "devprops":
            return next(iter(app.window._device_properties.values()), None)
        return app.window

    def size_windows():
        # the window sizes of the plan, whatever size a window would open at
        for kind, size in plan.get("sizes", {}).items():
            win = target_of(kind)
            if win is not None:
                w, _, h = size.partition("x")
                win.set_size_request(int(w), int(h))

    def prepare():
        if not queue:
            app.quit()
            return False
        for action in queue[0].get("actions", []):
            apply_action(app, action)
        size_windows()
        # the window of the shot in front, not dimmed (other windows of the plan may have opened later)
        focus_window(target_of(queue[0].get("target", "main")), lambda: GLib.timeout_add(settle, wait_busy, 0))
        return False

    def busy_enough():
        h = app.sampler.history
        want = queue[0].get("wait", {})
        if not h.cpu_total:
            return True
        n = min(want.get("samples", 1), len(h.cpu_total))  # for that many updates in a row
        cpu = list(h.cpu_total)[-n:]
        disk = [r + w for r, w in zip(list(h.disk_read)[-n:], list(h.disk_write)[-n:])]
        return min(cpu) >= want.get("cpu", 0) and min(disk) >= want.get("disk", 0)

    def wait_busy(waited):
        if not busy_enough() and waited < 120:
            GLib.timeout_add(250, wait_busy, waited + 1)
        else:
            # right away: in the next update the load the shot waited for may be gone
            GLib.timeout_add(200, shoot, 0)
        return False

    def shoot(attempt):
        shot = queue[0]
        target = target_of(shot.get("target", "main"))
        if attempt == 0 and shot.get("target", "main") == "main":
            # rows that appeared at the top since the last look have pushed the list down a bit
            app.window.process_view.view.get_vadjustment().set_value(0)
            GLib.timeout_add(150, shoot, 1)
            return False
        out = os.path.join(out_dir, f"{shot['name']}-{theme}.png")
        if target is None:
            print("screenshot skipped (no window)", out, flush=True)
        elif not render_widget(target, out, quiet=attempt < 20):
            # nothing to render until the window has drawn itself again after the actions
            target.queue_draw()
            GLib.timeout_add(250, shoot, attempt + 1)
            return False
        else:
            print("screenshot", out, flush=True)
        queue.pop(0)
        GLib.idle_add(prepare)
        return False

    def early():
        for action in plan.get("early", []):
            apply_action(app, action)
        size_windows()
        return False

    GLib.timeout_add(1500, early)
    GLib.timeout_add(int(plan.get("warmup", 60) * 1000), prepare)



def main():
    if not os.environ.get("PROCULAR_SCREENSHOT"):
        print(__doc__, file=sys.stderr)
        return 2
    from procular.app import ProcularApp
    from procular.util import process_name

    process_name.set_name("procular")
    app = ProcularApp()
    # an instance of its own, also next to a Procular running on the desktop
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(app):
        if not getattr(app, "_shots_installed", False):  # the main window exists from here on
            app._shots_installed = True
            install(app)

    app.connect_after("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
