"""Generic live-updating, sortable, filterable table on top of Gtk.ColumnView."""

import gi

gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402


class Row(GObject.Object):
    __gtype_name__ = "ProcularTableRow"
    __gsignals__ = {"updated": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, key, data):
        super().__init__()
        self.key = key
        self.data = data
        self.hl = ""
        self.hl_until = 0.0
        self.removed_at = None
        self.parent_key = None  # tree tables: key of the parent row, None at the top
        self.container = None  # tree tables: the list store this row is in
        self.children = None  # tree tables: Gio.ListStore of the child rows


def _row_of(item):
    """The Row behind a list item: tree tables hand out Gtk.TreeListRows."""
    return item.get_item() if isinstance(item, Gtk.TreeListRow) else item


class Column:
    def __init__(self, cid, title, get, fmt=None, numeric=False, width=None, expand=False,
                 sort=None, visible=True, icon=None, indicator=None, tooltip=None, markup=None, css=(),
                 ellipsize=Pango.EllipsizeMode.END, bar=None, heat=None):
        self.id = cid
        self.title = title
        self.get = get if callable(get) else (lambda d, k=get: d.get(k))
        self.fmt = fmt or (lambda v: "" if v is None else str(v))
        self.numeric = numeric
        self.width = width
        self.expand = expand
        self.sort = sort
        self.visible = visible
        self.icon = icon  # callable(data) -> Gio.Icon or None
        # callable(data) -> state name or None; draws a status dot styled by css class "status-<state>"
        self.indicator = indicator
        self.tooltip = tooltip  # callable(data) -> str or None
        # callable(value) -> Pango markup used for display only (fmt stays the plain text for
        # filtering and copying)
        self.markup = markup
        self.css = tuple(css)  # extra css classes for the cell label (e.g. "monospace-text")
        self.bar = bar  # callable(data) -> 0..1 or None; draws a small bar next to the text
        self.heat = heat  # callable(value) -> 0..5 background intensity on the cell label
        self.ellipsize = ellipsize


def _cmp(a, b):
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.casefold(), b.casefold()
    try:
        return (a > b) - (a < b)
    except TypeError:
        a, b = str(a), str(b)
        return (a > b) - (a < b)


def _children_of(row):
    # every row gets its list, also while it has no children: GTK asks only once (see _bind
    # for the expander arrow of the rows without children)
    return row.children


def find_ancestor_with(widget, attr):
    while widget is not None:
        if hasattr(widget, attr):
            return widget
        widget = widget.get_parent()
    return None


def set_row_class(cell_child, cls):
    """Put the css class hl-<cls> on the ColumnView row that contains cell_child.

    cls may name several classes separated by spaces, or none ("")."""
    if cell_child.get_root() is None:
        # The row is still being created: GTK binds the cells first and inserts the row into the
        # list afterwards. Until then the row is "floating", and a Python wrapper for it would
        # take over its only reference: dropping the wrapper frees the row under GTK's feet,
        # keeping it turns every row GTK later lets go of into a zombie that stays bound to its
        # item. So don't touch the row yet; do it right after the insertion.
        if getattr(cell_child, "_row_cls_pending", None) is None:
            GLib.idle_add(_set_pending_row_class, cell_child, priority=GLib.PRIORITY_HIGH)
        cell_child._row_cls_pending = cls
        return
    cell_child._row_cls_pending = None
    cell = cell_child.get_parent()
    row = cell.get_parent() if cell is not None else None
    if row is None:
        return
    old = getattr(cell_child, "_row_cls", "")
    if old and old != cls:
        for name in old.split():
            row.remove_css_class("hl-" + name)
    if cls:
        for name in cls.split():
            row.add_css_class("hl-" + name)
    cell_child._row_cls = cls


def _set_pending_row_class(cell_child):
    cls = getattr(cell_child, "_row_cls_pending", None)
    cell_child._row_cls_pending = None
    # still unrooted: the row was dropped again before it ever got into the list
    if cls is not None and cell_child.get_root() is not None:
        set_row_class(cell_child, cls)
    return False


_UNBLOCKED = GObject.SignalMatchType.ID | GObject.SignalMatchType.UNBLOCKED


def make_reorderable(view):
    """Let the user move the columns of `view` by dragging their headers.

    set_reorderable(True) alone does nothing (GTK 4.24): a column title claims every press for
    its click-to-sort gesture, which cancels the column view's drag gesture before the pointer
    has moved far enough for a drag. The title's "pressed" handler does nothing but claim, so
    it is blocked; a click still sorts on release and right-click still opens the header menu.
    Titles are created again whenever a column moves, so this is done on every press, before
    the press reaches the title."""
    view.set_reorderable(True)
    ctrl = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    ctrl.connect("event", _unclaim_titles)  # nothing in here refers to the view
    view.add_controller(ctrl)


def _unclaim_titles(ctrl, _event):
    event = ctrl.get_current_event()  # PyGObject passes the signal's event as None
    if event is None or event.get_event_type() != Gdk.EventType.BUTTON_PRESS:
        return False
    # looked up here: before the first GestureClick exists the signal is not registered (0)
    pressed = GObject.signal_lookup("pressed", Gtk.GestureClick)
    header = ctrl.get_widget().get_first_child()  # the row of column titles
    title = header.get_first_child() if header is not None else None
    while title is not None:
        gestures = title.observe_controllers()
        for i in range(gestures.get_n_items()):
            gesture = gestures.get_item(i)
            if isinstance(gesture, Gtk.GestureClick):
                handler = GObject.signal_handler_find(gesture, _UNBLOCKED, pressed, 0, None, None, None)
                if handler:
                    GObject.signal_handler_block(gesture, handler)
        title = title.get_next_sibling()
    return False


def remember_sort(view, config, name, default=None, descending=False):
    """Sort the ColumnView the way the user left the list `name`, and save how they sort it later.

    default, descending: the column id and direction for a list the user never sorted.
    Returns the handler connected to the view's sorter. Disconnect it before the view is
    disposed: a view that is freed drops its columns, which looks like the user un-sorting it."""
    columns = {}
    for i in range(view.get_columns().get_n_items()):
        column = view.get_columns().get_item(i)
        columns[column.get_id()] = column
    builtin = {"column": default, "descending": descending} if default else None
    state = config.get_sort(name, builtin)
    if state is not None and not (isinstance(state, dict) and state.get("column") in columns):
        state = builtin  # a column that no longer exists
    if state is None:
        view.sort_by_column(None, Gtk.SortType.ASCENDING)
    else:
        view.sort_by_column(columns[state["column"]],
                            Gtk.SortType.DESCENDING if state.get("descending") else Gtk.SortType.ASCENDING)
    last = [state]

    # no reference to the view in here: the sorter would keep it alive (see util.lifecycle)
    def on_changed(sorter, _change):
        column = sorter.get_primary_sort_column()
        if column is None:
            now = None
        else:
            now = {"column": column.get_id(),
                   "descending": sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING}
        # this also runs when the rows are merely sorted again (new data)
        if now != last[0]:
            last[0] = now
            config.set_sort(name, now)

    return view.get_sorter().connect("changed", on_changed)


def context_menu(menu_model, host):
    """A compact context menu on `host`, which must call present_popover() in do_size_allocate.

    GTK resizes a popover only when its parent presents it again while being allocated
    (GtkMenuButton, GtkLabel and GtkText do so; GtkColumnView does not). Without that the
    menu keeps the size it opened with, and a submenu taller than the main page scrolls."""
    popover = Gtk.PopoverMenu.new_from_model(menu_model)
    popover.add_css_class("context-menu")
    popover.set_parent(host)
    popover.set_has_arrow(False)
    popover.set_halign(Gtk.Align.START)
    return popover


def present_popover(popover):
    if popover is not None and popover.get_visible():
        popover.present()


def popup_at(popover, widget, x, y):
    """Open popover pointing at the point x, y of widget (a descendant of its parent)."""
    ok, point = widget.compute_point(popover.get_parent(), Graphene.Point().init(x, y))
    if ok:
        x, y = point.x, point.y
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    popover.set_pointing_to(rect)
    popover.popup()


class Table(Gtk.Box):
    """A scrolled ColumnView fed by set_rows(list_of_dicts).

    With parent_key (a key or callable giving the key of a row's parent, None for a top-level
    row), the rows form a tree: the first column gets expanders, siblings are sorted among
    themselves, and a filter keeps the ancestors of every match."""

    __gsignals__ = {
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, columns, key, filter_keys=None, menu_model=None, css_class="data-table", parent_key=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.columns = columns
        self.key = key if callable(key) else (lambda d, k=key: d[k])
        self.parent_key = None
        if parent_key is not None:
            self.parent_key = parent_key if callable(parent_key) else (lambda d, k=parent_key: d.get(k))
        self.tree = self.parent_key is not None
        self.filter_keys = filter_keys
        self._rows = {}
        self._filter_text = ""
        self._visible = None  # tree tables while filtering: keys of the matches and their ancestors
        self.store = Gio.ListStore(item_type=Row)  # tree tables: the top-level rows

        self.view = Gtk.ColumnView()
        self.view.add_css_class(css_class)
        make_reorderable(self.view)
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        self._sorters = {}
        for idx, col in enumerate(columns):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup, col, idx == 0)
            factory.connect("bind", self._bind, col, idx == 0)
            factory.connect("unbind", self._unbind)
            column = Gtk.ColumnViewColumn(title=col.title, factory=factory)
            column.set_resizable(True)
            column.set_id(col.id)
            if col.width:
                column.set_fixed_width(col.width)
            column.set_expand(col.expand)
            column.set_visible(col.visible)
            sorter = Gtk.CustomSorter.new(self._compare, col)
            self._sorters[col.id] = sorter
            column.set_sorter(sorter)
            self.view.append_column(column)

        if self.tree:
            # autoexpand: a row that appears is open, so a device that is plugged in is seen
            self.tree_model = Gtk.TreeListModel.new(self.store, False, True, _children_of)
            self.sort_model = Gtk.SortListModel.new(self.tree_model,
                                                    Gtk.TreeListRowSorter.new(self.view.get_sorter()))
        else:
            self.sort_model = Gtk.SortListModel.new(self.store, self.view.get_sorter())
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel.new(self.sort_model, self.filter)
        self.selection = Gtk.SingleSelection.new(self.filter_model)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.connect("notify::selected", lambda *_: self.emit("selection-changed"))
        self.view.set_model(self.selection)
        self.view.connect("activate", self._on_activate)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.view)
        scroller.set_vexpand(True)
        self.append(scroller)

        self.popover = None
        if menu_model is not None:
            self.set_menu(menu_model)

    # ----- cells
    def _setup(self, _factory, list_item, col, first):
        label = Gtk.Label(single_line_mode=True)
        label.set_ellipsize(col.ellipsize)
        label.set_xalign(1.0 if col.numeric else 0.0)
        if col.numeric:
            label.add_css_class("numeric")
        for cls in col.css:
            label.add_css_class(cls)
        if col.icon is not None or col.indicator is not None or col.bar is not None:
            box = Gtk.Box(spacing=6)
            if col.bar is not None:
                bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, hexpand=True,
                                   css_classes=["cell-bar"])
                for offset in ("low", "high", "full"):
                    bar.remove_offset_value(offset)
                box.append(bar)
                box._bar = bar
            if col.indicator is not None:
                dot = Gtk.Box(css_classes=["status-dot"], valign=Gtk.Align.CENTER)
                box.append(dot)
                box._dot = dot
            if col.icon is not None:
                image = Gtk.Image()
                image.set_pixel_size(16)
                box.append(image)
                box._image = image
            box.append(label)
            box._label = label
            cell = box
        else:
            cell = label
        if first and self.tree:
            expander = Gtk.TreeExpander(child=cell)
            expander.set_indent_for_icon(True)
            expander._label = label
            for name in ("_image", "_dot", "_bar"):
                if hasattr(cell, name):
                    setattr(expander, name, getattr(cell, name))
            cell = expander
        list_item.set_child(cell)

    def _bind(self, _factory, list_item, col, first):
        child = list_item.get_child()
        item = list_item.get_item()
        row = _row_of(item)
        child._item = row
        expander = child if isinstance(child, Gtk.TreeExpander) else None
        if expander is not None:
            expander.set_list_row(item)
        # the cell is the label itself or a box holding it (see _setup)
        label = getattr(child, "_label", child)

        def refresh(*_):
            data = row.data
            value = col.get(data)
            if col.markup is not None:
                label.set_markup(col.markup(value))
            else:
                label.set_text(col.fmt(value))
            if col.heat is not None:
                level = col.heat(value) if value is not None else 0
                old = getattr(label, "_heat", 0)
                if old != level:
                    if old:
                        label.remove_css_class(f"heat-{old}")
                    if level:
                        label.add_css_class(f"heat-{level}")
                    label._heat = level
            if col.tooltip is not None:
                child.set_tooltip_text(col.tooltip(data))
            if col.icon is not None:
                # only when it changed: a newly set icon may be loaded from disk again on the next draw
                icon = col.icon(data)
                if icon is not getattr(child, "_icon", None):
                    child._icon = icon
                    if icon is not None:
                        child._image.set_from_gicon(icon)
                    else:
                        child._image.clear()
            if col.bar is not None:
                child._bar.set_value(min(1.0, max(0.0, col.bar(data) or 0.0)))
            if col.indicator is not None:
                state = col.indicator(data)
                child._dot.set_css_classes(["status-dot", f"status-{state}"] if state else ["status-dot"])
            if expander is not None:
                expander.set_hide_expander(row.children.get_n_items() == 0)
            if first:
                set_row_class(child, row.hl)

        refresh()
        child._hid = row.connect("updated", refresh)

    def _unbind(self, _factory, list_item):
        child = list_item.get_child()
        row = _row_of(list_item.get_item())
        if isinstance(child, Gtk.TreeExpander):
            child.set_list_row(None)
        hid = getattr(child, "_hid", 0)
        if hid and row is not None:
            row.disconnect(hid)
        child._hid = 0
        child._item = None
        if getattr(child, "_row_cls", ""):
            set_row_class(child, "")
        label = getattr(child, "_label", child)
        old_heat = getattr(label, "_heat", 0)
        if old_heat:
            label.remove_css_class(f"heat-{old_heat}")
            label._heat = 0

    def _compare(self, a, b, col):
        if col.sort is not None:
            return _cmp(col.sort(a.data), col.sort(b.data))
        return _cmp(col.get(a.data), col.get(b.data))

    def _filter_func(self, item):
        if not self._filter_text:
            return True
        if self._visible is not None:
            return _row_of(item).key in self._visible
        return self._matches(item)

    def _matches(self, row):
        text = self._filter_text
        data = row.data
        if self.filter_keys:
            for k in self.filter_keys:
                value = k(data) if callable(k) else data.get(k)
                if value is not None and text in str(value).casefold():
                    return True
            return False
        for col in self.columns:
            if text in col.fmt(col.get(data)).casefold():
                return True
        return False

    # ----- data
    def set_rows(self, rows, highlight=None, now=None):
        """Replace content. Rows are matched by key so selection survives.

        highlight: seconds to flash added (green) / removed (red) rows, or None.
        self.hl_func(data) can provide a steady-state highlight class.
        """
        seen = set()
        added = []
        moved = []
        sort_dirty = filter_dirty = False
        filtering = bool(self._filter_text)
        base = self.hl_func or (lambda _d: "")
        for data in rows:
            key = self.key(data)
            seen.add(key)
            row = self._rows.get(key)
            if self.tree and row is not None and row.parent_key != self.parent_key(data):
                moved.append((row, row.parent_key))
                row.parent_key = self.parent_key(data)
            if row is None:
                row = Row(key, data)
                if self.tree:
                    row.parent_key = self.parent_key(data)
                    row.children = Gio.ListStore(item_type=Row)
                if highlight and now is not None and self._populated:
                    row.hl = "new"
                    row.hl_until = now + highlight
                else:
                    row.hl = base(data)
                self._rows[key] = row
                added.append(row)
                continue
            # every bound cell runs Python to redraw: only rows that changed are told to
            changed = False
            if row.removed_at is not None:
                row.removed_at = None
                row.hl_until = 0
                changed = True
            if row.data != data:
                # GTK only filters a row again when told to: a changed row may match now, or no more
                matched = filtering and self._matches(row)
                row.data = data
                sort_dirty = changed = True
                if filtering and self._matches(row) != matched:
                    filter_dirty = True
            if not row.hl_until or (now is not None and now >= row.hl_until):
                row.hl_until = 0
                hl = base(data)
                if hl != row.hl:
                    row.hl = hl
                    changed = True
            if changed:
                row.emit("updated")
        if self.tree:
            grown = self._place(added, moved)
            # the ancestors of the matches may be others now
            filter_dirty = filter_dirty or (filtering and bool(added or moved))
        elif added:
            self.store.splice(self.store.get_n_items(), 0, added)
        removed = [k for k in self._rows if k not in seen]
        for key in removed:
            row = self._rows[key]
            if highlight and now is not None:
                if row.removed_at is None:
                    row.removed_at = now
                    row.hl = "term"
                    row.hl_until = now + highlight
                    row.emit("updated")
                    continue
                if now - row.removed_at < highlight:
                    continue
            container = row.container if self.tree else self.store
            if container is not None:
                found, pos = container.find(row)
                if found:
                    container.remove(pos)
                if self.tree:
                    grown.add(row.parent_key)
                    filter_dirty = filter_dirty or filtering
            del self._rows[key]
        self._populated = True
        if self.tree:
            # a row that got its first child or lost its last one shows or hides its expander
            for key in grown:
                parent = self._rows.get(key)
                if parent is not None:
                    parent.emit("updated")
        if sort_dirty:
            self.resort()
        if filter_dirty:
            self._refilter()

    def _place(self, added, moved):
        """Put new and re-parented rows into their parent's children (a missing parent: the top).
        Returns the keys of the parents that gained or lost children."""
        changed = set()
        moved_rows = []
        for row, old_parent in moved:
            if row.container is not None:
                found, pos = row.container.find(row)
                if found:
                    row.container.remove(pos)
            changed.add(old_parent)
            moved_rows.append(row)
        batches = {}
        for row in added + moved_rows:
            parent = self._rows.get(row.parent_key) if row.parent_key is not None else None
            container = parent.children if parent is not None else self.store
            row.container = container
            batches.setdefault(id(container), (container, []))[1].append(row)
            changed.add(row.parent_key)
        for container, batch in batches.values():
            container.splice(container.get_n_items(), 0, batch)
        return changed

    def _refilter(self):
        if self.tree and self._filter_text:
            visible = set()
            for row in self._rows.values():
                if row.key not in visible and self._matches(row):
                    key = row.key
                    while key is not None and key not in visible:
                        visible.add(key)
                        parent = self._rows.get(key)
                        key = parent.parent_key if parent is not None else None
            self._visible = visible
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
            self._expand(visible)  # a match inside a closed branch
        else:
            self._visible = None
            self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _expand(self, keys):
        """Open the tree rows whose key is in keys (rows opened by this one included)."""
        i = 0
        while i < self.tree_model.get_n_items():
            tree_row = self.tree_model.get_item(i)
            if not tree_row.get_expanded() and tree_row.get_item().key in keys:
                tree_row.set_expanded(True)
            i += 1

    _populated = False
    _sort_saver = 0
    hl_func = None

    def set_row_hl(self, key, hl):
        row = self._rows.get(key)
        if row is not None and row.hl != hl:
            row.hl = hl
            row.emit("updated")

    def resort(self):
        sorter = self.view.get_sorter()
        col = sorter.get_primary_sort_column() if hasattr(sorter, "get_primary_sort_column") else None
        if col is not None:
            self._sorters[col.get_id()].changed(Gtk.SorterChange.DIFFERENT)

    def set_filter_text(self, text):
        text = (text or "").casefold().strip()
        if text != self._filter_text:
            self._filter_text = text
            self._refilter()

    def clear(self):
        self.store.remove_all()
        self._rows.clear()
        self._populated = False

    def teardown(self):
        """Called by util.lifecycle.destroy_tree() when the window holding the table is freed."""
        if self._sort_saver:
            self.view.get_sorter().disconnect(self._sort_saver)
            self._sort_saver = 0
        self.view.set_model(None)  # unbinds and drops the cell widgets
        if self.popover is not None:
            self.popover.unparent()  # parented by hand in set_menu()
        self.clear()

    def remember_sort(self, config, name, default=None, descending=False):
        """Restore and keep saving the sort column under `name`; see remember_sort()."""
        self._sort_saver = remember_sort(self.view, config, name, default, descending)

    def selected(self):
        row = self.selected_row()
        return row.data if row is not None else None

    def selected_row(self):
        item = self.selection.get_selected_item()
        return _row_of(item) if item is not None else None

    def all_rows(self, removed=True):
        """The data of every row; removed=False: without the rows that linger after removal."""
        return [r.data for r in self._rows.values() if removed or r.removed_at is None]

    def _on_activate(self, _view, position):
        item = self.filter_model.get_item(position)
        if item is not None:
            self.emit("activated", _row_of(item).data)

    # ----- context menu
    def set_menu(self, menu_model):
        """The context menu. Its model keeps its structure from here on: no section may turn
        empty or non-empty. GtkPopoverMenu shows a section's separator in an idle callback, and
        a menu that pops up before it runs (right-click selects the row, which updates the menu,
        then opens it) is measured without it and cuts off its last item."""
        self.popover = context_menu(menu_model, self)
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_right_click)
        self.view.add_controller(gesture)

    def do_size_allocate(self, width, height, baseline):
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        present_popover(self.popover)

    def _on_right_click(self, gesture, _n, x, y):
        widget = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        holder = find_ancestor_with(widget, "_item")
        if holder is None or holder._item is None:
            return
        self.select_row(holder._item)
        popup_at(self.popover, self.view, x, y)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def select_row(self, row):
        for i in range(self.filter_model.get_n_items()):
            if _row_of(self.filter_model.get_item(i)) is row:
                self.selection.set_selected(i)
                return True
        return False

    def select_key(self, key):
        row = self._rows.get(key)
        if row is not None and self.tree:
            ancestors = set()
            parent = self._rows.get(row.parent_key) if row.parent_key is not None else None
            while parent is not None and parent.key not in ancestors:
                ancestors.add(parent.key)
                parent = self._rows.get(parent.parent_key) if parent.parent_key is not None else None
            self._expand(ancestors)
        if row is not None and self.select_row(row):
            pos = self.selection.get_selected()
            self.view.scroll_to(pos, None, Gtk.ListScrollFlags.FOCUS, None)
            return True
        return False

    def copy_selected(self):
        data = self.selected()
        if data is None:
            return
        text = "\t".join(c.fmt(c.get(data)) for c in self.columns)
        Gdk.Display.get_default().get_clipboard().set(text)
