import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from procular.app import ProcularApp
from procular.config import Config
from procular.models.process_tree import ProcessTree
from procular.widgets import devices_view, network_view, services_view
from procular.widgets.process_view import ProcessView
from procular.widgets.table import Column, Table
from procular.windows.main_window import MainWindow


@pytest.fixture
def process_view(gtk, tmp_path):
    config = Config(path=str(tmp_path / "settings.json"))
    tree = ProcessTree(config)
    sampler = SimpleNamespace(scanner=SimpleNamespace(), system=SimpleNamespace(gpu=SimpleNamespace()))
    view = ProcessView(SimpleNamespace(my_uid=os.getuid()), tree, sampler, config, None)
    base = {"starttime": 1, "kthread": False, "uid": os.getuid(), "state": "S"}
    tree.apply({10: dict(base, pid=10, ppid=0, name="parent"),
                11: dict(base, pid=11, ppid=10, name="matching-child"),
                12: dict(base, pid=12, ppid=10, name="other-child")})
    yield view
    config.save_now()


def shown_pids(view):
    model = view.filter_model
    return [model.get_item(i).get_item().pid for i in range(model.get_n_items())]


def test_search_finds_children_of_collapsed_rows_and_restores_expansion(process_view):
    view = process_view
    view.expand_all(False)
    assert shown_pids(view) == [10]
    view.set_filter_text("matching-child")
    assert shown_pids(view) == [10, 11]
    view.set_filter_text("")
    assert shown_pids(view) == [10]


def test_switching_views_refilters_and_preserves_selection_without_a_tick(process_view):
    view = process_view
    view.set_filter_text("matching-child")
    item = view.tree.get(11)
    view._select_item(item, scroll=False)
    view.set_tree_mode(False)
    assert shown_pids(view) == [11]
    assert view.selected_item() is item
    view.set_tree_mode(True)
    assert shown_pids(view) == [10, 11]
    assert view.selected_item() is item


@pytest.mark.parametrize("page_name", ["services", "network", "devices", "processes"])
def test_manual_refresh_reaches_only_the_active_polled_page_even_paused(page_name):
    pages = {name: Mock() for name in ("services", "network", "devices", "processes")}
    win = SimpleNamespace(current_page=lambda: pages[page_name], services_view=pages["services"],
                          network_view=pages["network"], devices_view=pages["devices"])
    win.refresh_active_page = lambda: MainWindow.refresh_active_page(win)
    sampler = SimpleNamespace(paused=True, refresh=Mock())
    ProcularApp._on_refresh(SimpleNamespace(sampler=sampler, window=win))
    sampler.refresh.assert_called_once_with()
    for name, page in pages.items():
        if name == page_name and name != "processes":
            page.refresh.assert_called_once_with(force=True)
        else:
            page.refresh.assert_not_called()


@pytest.mark.parametrize("module, cls, on_result", [
    (services_view, services_view.ServicesView, "_on_units"),
    (network_view, network_view.NetworkView, "_on_conns"),
    (devices_view, devices_view.DevicesView, "_on_loaded"),
])
def test_forced_list_refresh_queues_a_single_followup(monkeypatch, module, cls, on_result):
    callbacks = []
    monkeypatch.setattr(module, "run_async", lambda _fn, done, *_args: callbacks.append(done))
    view = SimpleNamespace(_active=True, _loading=False, _refresh_again=False, _extra_owners={},
                           _scopes=lambda: ("system",), _unit_cgroups=dict,
                           provider=SimpleNamespace(list_units=Mock()),
                           _static=None, _elevated=None, show_all=SimpleNamespace(get_active=lambda: False))
    view.refresh = lambda **kwargs: cls.refresh(view, **kwargs)
    setattr(view, on_result, lambda *args: getattr(cls, on_result)(view, *args))
    view.refresh()
    view.refresh(force=True)
    view.refresh(force=True)
    assert len(callbacks) == 1
    callbacks[0](None, None)
    assert len(callbacks) == 2
    callbacks[1](None, None)
    assert not view._loading and len(callbacks) == 2


def test_services_searches_by_scope_and_startup_state(gtk):
    table = Table([Column("name", "Name", "name")], key="name", filter_keys=services_view.SERVICE_FILTER_FIELDS)
    table.set_rows([{"name": "a.service", "scope": "system", "enabled": "disabled"},
                    {"name": "b.service", "scope": "user", "enabled": "enabled"}])
    for query in ("user", "enabled"):
        table.set_filter_text(query)
        assert table.filter_model.get_n_items() == 1
        assert table.filter_model.get_item(0).data["name"] == "b.service"


def test_network_search_matches_copied_ipv4_and_ipv6_endpoints(gtk):
    table = Table([Column("pid", "PID", "pid")], key="pid", filter_keys=network_view.NETWORK_FILTER_FIELDS)
    table.set_rows([{"pid": 10, "laddr": "127.0.0.1", "lport": 8080, "raddr": "::1", "rport": 443},
                    {"pid": 11, "laddr": "127.0.0.1", "lport": 8081, "raddr": "::1", "rport": 9000}])
    for query in ("127.0.0.1:8080", "[::1]:443"):
        table.set_filter_text(query)
        assert table.filter_model.get_n_items() == 1
        assert table.filter_model.get_item(0).data["pid"] == 10


def test_unbinding_a_cell_clears_its_not_readable_look(process_view):
    # a recycled cell's next binding starts from scratch: an "n/a" look kept from the previous
    # process would dim a readable value and give it the "Not readable" tooltip
    from gi.repository import Gtk
    label = Gtk.Label(label="n/a")
    label.add_css_class("na")
    label.set_tooltip_text("Not readable")
    process_view._unbind(None, SimpleNamespace(get_child=lambda: label))
    assert not label.has_css_class("na") and label.get_tooltip_text() is None


def test_table_redraws_only_rows_that_changed(gtk):
    table = Table([Column("name", "Name", "name")], key="name")
    table.hl_func = lambda d: "hot" if d.get("hot") else ""
    table.set_rows([{"name": "a"}, {"name": "b"}])
    emitted = []
    for row in table._rows.values():
        row.connect("updated", lambda r: emitted.append(r.key))
    table.set_rows([{"name": "a"}, {"name": "b"}])
    assert emitted == []  # nothing changed: no cell runs its refresh
    table.set_rows([{"name": "a", "hot": True}, {"name": "b"}])
    assert emitted == ["a"]
    assert table._rows["a"].hl == "hot"


def _tree_table():
    table = Table([Column("name", "Name", "name")], key="name", parent_key="parent")
    table.set_rows([{"name": "pci", "parent": None}, {"name": "usb", "parent": "pci"},
                    {"name": "mouse", "parent": "usb"}, {"name": "disk", "parent": "pci"},
                    {"name": "platform", "parent": None}])
    return table


def shown_keys(table):
    return [table.filter_model.get_item(i).get_item().key for i in range(table.filter_model.get_n_items())]


def test_tree_table_nests_rows_and_sorts_siblings(gtk):
    from gi.repository import Gtk
    table = _tree_table()
    table.view.sort_by_column(table.view.get_columns().get_item(0), Gtk.SortType.ASCENDING)
    # every branch open, children right below their parent, siblings in order
    assert shown_keys(table) == ["pci", "disk", "usb", "mouse", "platform"]
    assert [r.key for r in table.store] == ["pci", "platform"]
    assert [r.key for r in table._rows["pci"].children] == ["usb", "disk"]


def test_tree_table_moves_and_removes_rows(gtk):
    table = _tree_table()
    # the mouse moved to another port; the disk is gone, and the usb branch with what hung below it
    table.set_rows([{"name": "pci", "parent": None}, {"name": "mouse", "parent": "platform"},
                    {"name": "platform", "parent": None}])
    assert [r.key for r in table._rows["pci"].children] == []
    assert [r.key for r in table._rows["platform"].children] == ["mouse"]
    assert set(table._rows) == {"pci", "platform", "mouse"}
    assert table._rows["mouse"].container is table._rows["platform"].children


def test_tree_table_filter_keeps_ancestors_and_opens_closed_branches(gtk):
    table = _tree_table()
    table.tree_model.get_item(0).set_expanded(False)  # close "pci"
    assert shown_keys(table) == ["pci", "platform"]
    table.set_filter_text("mouse")
    assert shown_keys(table) == ["pci", "usb", "mouse"]
    assert table.selected() is None
    assert table.select_key("mouse")
    assert table.selected()["name"] == "mouse"
    table.set_filter_text("")
    assert "platform" in shown_keys(table)
    # a row that starts to match while filtering is found
    table.set_filter_text("keyboard")
    assert shown_keys(table) == []
    table.set_rows([{"name": "pci", "parent": None}, {"name": "usb", "parent": "pci"},
                    {"name": "keyboard", "parent": "usb"}, {"name": "platform", "parent": None}])
    assert shown_keys(table) == ["pci", "usb", "keyboard"]


def test_tree_table_select_key_opens_the_branch(gtk):
    table = _tree_table()
    table.tree_model.get_item(0).set_expanded(False)
    assert table.select_key("mouse")
    assert table.selected_row().key == "mouse"
