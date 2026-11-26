"""End-to-end tests of the running app: the real main window, driven in this process.

They run only in the off-screen session, never on the desktop (windows would pop up and take
the focus); elsewhere they are skipped:
    tools/headless-session.sh python3 -m pytest tests/test_ui.py

One app instance serves the whole module; it monitors this machine. The tests send signals only
to processes they started themselves. A test also fails when GTK or libadwaita log a warning or
a callback raises while it runs (the exception would otherwise only be printed).
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.ui

PROBLEMS = re.compile(r"Traceback|(Gtk|Adwaita)-(WARNING|CRITICAL)")
SETTINGS = {"interval_ms": 500, "background_slowdown": False, "confirm_kill": False, "highlight_seconds": 3,
            "about_music": False}


def run_main_loop():
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(1000):  # bounded: a running animation always has work pending
        if not context.iteration(False):
            break


def wait_until(condition, timeout=10.0, what=None):
    """Run the main loop until condition() is true and return its value; fail after timeout s."""
    deadline = time.monotonic() + timeout
    while True:
        run_main_loop()
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:g} s waiting for {what or condition.__name__}")
        time.sleep(0.02)


def start_app(config_path):
    import procular  # noqa: F401  (pins the Gtk 4 typelib)
    from gi.repository import Gtk  # first: it picks the Gdk version
    from gi.repository import Gdk, Gio

    if not os.environ.get("PROCULAR_HEADLESS"):
        pytest.skip("only in the off-screen session: tools/headless-session.sh python3 -m pytest")
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("no display")
    from procular.app import ProcularApp

    config_path.write_text(json.dumps(SETTINGS))
    os.environ["PROCULAR_CONFIG"] = str(config_path)
    app = ProcularApp()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)  # next to a Procular on the desktop
    assert app.register(None)
    app.activate()
    wait_until(lambda: app.window is not None and app.window.get_mapped(), what="the main window")
    wait_until(lambda: os.getpid() in app.tree.items, what="the first update")
    return app


def stop_app(app):
    from gi.repository import GLib, Gtk

    sampler = app.sampler
    sampler.set_paused(True)
    if sampler._timer:
        GLib.source_remove(sampler._timer)
        sampler._timer = 0
    wait_until(lambda: not sampler._busy, what="the last update")
    for window in list(Gtk.Window.list_toplevels()):
        window.destroy()
    run_main_loop()
    os.environ.pop("PROCULAR_CONFIG", None)


@pytest.fixture(scope="module")
def _instance():
    holder = {}
    yield holder
    if "app" in holder:
        stop_app(holder["app"])


@pytest.fixture
def app(_instance, capfd, tmp_path_factory):
    """The running app. Started by the first test that needs it, so that its start is checked, too."""
    if "skip" in _instance:
        pytest.skip(_instance["skip"])
    if "app" not in _instance:
        try:
            _instance["app"] = start_app(tmp_path_factory.mktemp("ui") / "settings.json")
        except pytest.skip.Exception as e:
            _instance["skip"] = str(e)
            raise
    yield _instance["app"]
    out, err = capfd.readouterr()
    sys.stdout.write(out)  # still shown with the test's report
    sys.stderr.write(err)
    problems = [line for line in err.splitlines() if PROBLEMS.search(line)]
    if problems:
        pytest.fail("logged while the test ran:\n" + "\n".join(problems), pytrace=False)


def spawn(*argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL)


def end(*procs):
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def child():
    proc = spawn("sleep", "1000")
    yield proc
    end(proc)


def item_of(app, proc):
    return wait_until(lambda: app.tree.get(proc.pid), what=f"process {proc.pid} in the tree")


def test_new_process_appears_highlighted_below_its_parent(app, child):
    it = item_of(app, child)
    assert it.hl == "new"
    assert it.parent is not None and it.parent.pid == os.getpid()
    view = app.window.process_view
    assert view.select_pid(child.pid)
    assert view.selected_item() is it


def test_suspend_resume_and_renice(app, child):
    view = app.window.process_view
    it = item_of(app, child)
    assert view.select_pid(child.pid)
    view.actions.activate_action("suspend", None)
    wait_until(lambda: it.rec["state"] == "T", what="state T")
    # once the green of a new process has gone
    wait_until(lambda: it.hl == "stopped", timeout=SETTINGS["highlight_seconds"] + 5, what="the stopped highlight")
    view.actions.activate_action("resume", None)
    wait_until(lambda: it.rec["state"] == "S", what="state S")
    from gi.repository import GLib

    view.actions.activate_action("nice", GLib.Variant.new_int32(10))
    wait_until(lambda: it.rec["nice"] == 10, what="nice 10")


def test_search_keeps_matches_and_their_ancestors(app, child):
    win, view = app.window, app.window.process_view
    other = spawn("sleep", "2000")
    try:
        item_of(app, child)
        item_of(app, other)
        win.search_bar.set_search_mode(True)
        win.search_entry.set_text("sleep 1000")
        wait_until(lambda: view._filter_text == "sleep 1000", what="the search")  # applied after a pause
        visible = view._visible
        assert child.pid in visible
        # the path from the top of the tree to the match (not always PID 1: in a container a
        # process started by "docker exec" has no parent inside it)
        path = {a.pid for a in app.tree.get(child.pid).ancestors()}
        assert os.getpid() in path and path <= visible, sorted(path - visible)
        assert other.pid not in visible
        assert view.filter_model.get_n_items() == len(visible)
    finally:
        win.clear_search()
        end(other)


def test_properties_window_loads_threads_and_open_files(app, child):
    win = app.window
    win.open_properties(item_of(app, child))
    pw = wait_until(lambda: win._properties.get(child.pid), what="the Properties window")
    try:
        pw.stack.set_visible_child_name("threads")
        page = pw.pages["threads"][1]
        wait_until(lambda: page.get_visible_child_name() == "content", what="the threads page")
        pw.stack.set_visible_child_name("files")
        page = pw.pages["files"][1]
        wait_until(lambda: page.get_visible_child_name() == "content", what="the open files page")
        assert page.table.store.get_n_items() >= 3  # stdin, stdout, stderr
    finally:
        pw.close()


def test_killed_process_turns_red_and_goes_into_the_history(app, child):
    from procular.widgets import process_actions as pa

    win, view = app.window, app.window.process_view
    pa.send_signal(win, [item_of(app, child)], signal.SIGKILL)
    child.wait()
    wait_until(lambda: (it := app.tree.get(child.pid)) is not None and it.hl == "term", what="the red highlight")
    assert view.select_pid(child.pid)
    enabled = [n for n in ("kill", "terminate", "nice") if view.actions.lookup_action(n).get_enabled()]
    assert not enabled, "actions on an exited process"
    view.selection.unselect_all()
    assert child.pid in [r["pid"] for r in app.tree.exit_history]
    wait_until(lambda: app.tree.get(child.pid) is None, timeout=SETTINGS["highlight_seconds"] + 5,
               what="the row to go")


# three processes; the second one has threads: priorities are per thread on Linux and must reach all
THREADED = ("import threading, time\n"
            "for _ in range(3): threading.Thread(target=time.sleep, args=(1001,), daemon=True).start()\n"
            "time.sleep(1001)")


@pytest.fixture
def trio(app):
    from gi.repository import Gtk

    procs = [spawn("sleep", "1001"), spawn(sys.executable, "-c", THREADED), spawn("sleep", "1001")]
    view = app.window.process_view
    view.view.sort_by_column(view._columns["cpu"], Gtk.SortType.DESCENDING)  # rows move every update
    items = [item_of(app, p) for p in procs]
    yield procs, items
    view.selection.unselect_all()
    view.view.sort_by_column(None, Gtk.SortType.ASCENDING)
    end(*procs)


def select_only(view, procs, items):
    """Select items, and make sure that the selection holds nothing else: this is a real session."""
    view._select_items(items)
    selected = {i.pid for i in view.selected_items()}
    assert selected == {i.pid for i in items} and selected <= {p.pid for p in procs}, sorted(selected)


def test_menu_on_a_multiple_selection(app, trio):
    view = app.window.process_view
    procs, items = trio
    picked = items[:2]
    select_only(view, procs, picked)
    view.popup_menu(picked[1], 40, 0)
    try:
        assert set(view.selected_items()) == set(picked), "right-click on a selected row keeps the selection"
        assert view._menu_title.get_label() == "2 processes"
        enabled = {n: view.actions.lookup_action(n).get_enabled()
                   for n in ("properties", "open-location", "goto-parent", "terminate", "nice", "copy-pid")}
        assert enabled == {"properties": False, "open-location": False, "goto-parent": False,
                           "terminate": True, "nice": True, "copy-pid": True}
    finally:
        view.popover.popdown()
    view.popup_menu(items[2], 40, 0)
    try:
        assert view.selected_items() == [items[2]], "right-click elsewhere selects just that row"
    finally:
        view.popover.popdown()


def test_actions_apply_to_every_selected_process(app, trio):
    from gi.repository import GLib

    view = app.window.process_view
    procs, items = trio
    select_only(view, procs, items[:2])
    view.actions.activate_action("suspend", None)
    view.actions.activate_action("nice", GLib.Variant.new_int32(12))
    wait_until(lambda: [i.rec["state"] for i in items] == ["T", "T", "S"], what="two suspended processes")
    wait_until(lambda: [i.rec["nice"] for i in items[:2]] == [12, 12], what="two reniced processes")
    assert items[2].rec["nice"] != 12
    assert set(view.selected_items()) == set(items[:2]), "the selection survives re-sorting"
    pid = procs[1].pid
    thread_nices = []
    for tid in os.listdir(f"/proc/{pid}/task"):
        with open(f"/proc/{pid}/task/{tid}/stat", "rb") as fh:
            thread_nices.append(int(fh.read().rsplit(b")", 1)[1].split()[16]))
    assert len(thread_nices) == 4 and set(thread_nices) == {12}, thread_nices
    view.actions.activate_action("resume", None)


def test_terminating_several_processes_asks_first(app, trio):
    from gi.repository import Adw

    win, view = app.window, app.window.process_view
    procs, items = trio
    select_only(view, procs, items)
    view.actions.activate_action("terminate", None)
    dialog = wait_until(win.get_visible_dialog, what="the confirmation")  # even with confirm_kill off
    assert isinstance(dialog, Adw.AlertDialog)
    dialog.emit("response", "ok")
    dialog.force_close()
    codes = []
    for p in procs:
        try:
            codes.append(p.wait(timeout=5))
        except subprocess.TimeoutExpired:
            codes.append(None)
    assert codes == [-signal.SIGTERM] * 3


def show_page(app, name):
    app.window.stack.set_visible_child_name(name)


@pytest.mark.skipif(not os.path.isdir("/run/systemd/system"), reason="no systemd")
def test_services_page_lists_units(app):
    show_page(app, "services")
    table = app.window.services_view.table
    wait_until(lambda: table.store.get_n_items() > 10, what="the units")


def test_network_page_lists_sockets(app):
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        show_page(app, "network")
        table = app.window.network_view.table
        wait_until(lambda: table.store.get_n_items() > 0, what="the sockets")


@pytest.mark.skipif(not os.path.isdir("/sys/block") or not os.listdir("/sys/block"), reason="no block devices")
def test_disk_page_lists_devices(app):
    show_page(app, "disk")
    store = app.window.disk_view.devices.store
    wait_until(lambda: store.get_n_items() > 0, what="the disks")


def test_devices_page_shows_a_tree(app):
    from procular.providers.devices import ROOT_KEY

    show_page(app, "devices")
    rows = app.window.devices_view.table._rows
    wait_until(lambda: ROOT_KEY in rows and len(rows) > 1, what="the device tree")
    assert rows[ROOT_KEY].children.get_n_items() > 0, "devices hang below the computer"


def test_device_properties_read_the_driver(app):
    win = app.window
    show_page(app, "devices")
    rows = win.devices_view.table._rows
    wait_until(lambda: len(rows) > 1, what="the device tree")
    bound = [key for key, row in rows.items() if row.data["bus"] == "pci" and row.data["driver"]]
    if not bound:
        pytest.skip("no PCI device with a driver")
    win.open_device_properties(bound[0])
    dw = wait_until(lambda: win._device_properties.get(bound[0]), what="the Properties window")
    try:
        driver = dw.pages["general"].rows["driver"]
        wait_until(lambda: driver.get_visible() and driver.value.get_text() == dw.row["driver"],
                   what="the driver row")
        dw.stack.set_visible_child_name("attributes")
        table = dw.pages["attributes"].table
        wait_until(lambda: len(table.all_rows()) > 10, what="the attributes")
    finally:
        dw.close()


def test_system_information_window(app):
    show_page(app, "processes")
    app.activate_action("sysinfo", None)
    wait_until(lambda: app.sysinfo_window is not None and app.sysinfo_window.get_mapped(), what="the window")
    history = app.sampler.history
    wait_until(lambda: len(history.cpu_total) >= 10, what="ten updates in the history")
    app.sysinfo_window.close()


def test_table_filter_follows_changed_rows(gtk):
    # GTK does not filter rows again when their data changes; the table has to
    from procular.widgets.table import Column, Table

    table = Table([Column("name", "Name", "name"), Column("sub", "State", "sub")], key="name")
    table.set_rows([{"name": "a", "sub": "running"}, {"name": "b", "sub": "dead"}])
    table.set_filter_text("running")
    table.set_rows([{"name": "a", "sub": "dead"}, {"name": "b", "sub": "running"}])
    shown = [table.filter_model.get_item(i).data["name"] for i in range(table.filter_model.get_n_items())]
    table.teardown()
    assert shown == ["b"]


def test_tree_table_notifies_old_parent_when_child_moves(gtk):
    from procular.widgets.table import Column, Table

    table = Table([Column("name", "Name", "name")], key="key", parent_key="parent")
    table.set_rows([
        {"key": "a", "parent": None, "name": "A"},
        {"key": "b", "parent": None, "name": "B"},
        {"key": "c", "parent": "a", "name": "C"},
    ])
    updated = []
    table._rows["a"].connect("updated", lambda _r: updated.append("a"))
    table._rows["b"].connect("updated", lambda _r: updated.append("b"))
    table.set_rows([
        {"key": "a", "parent": None, "name": "A"},
        {"key": "b", "parent": None, "name": "B"},
        {"key": "c", "parent": "b", "name": "C"},
    ])
    assert table._rows["a"].children.get_n_items() == 0
    assert table._rows["b"].children.get_n_items() == 1
    assert "a" in updated and "b" in updated
    table.teardown()
