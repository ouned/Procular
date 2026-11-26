"""A container that wraps its children like words in a paragraph.

Stands in for Adw.WrapBox (libadwaita >= 1.7) so Procular runs with libadwaita 1.5.
Gtk.FlowBox is no replacement: it lines its children up in columns as wide as the widest
child, so short and long "chips" end up with big gaps between them.
"""

from gi.repository import Gdk, Gtk


class WrapLayout(Gtk.LayoutManager):
    """Children keep their natural width and flow left to right, starting a new line when full."""

    def __init__(self, child_spacing=0, line_spacing=0):
        super().__init__()
        self.child_spacing = child_spacing
        self.line_spacing = line_spacing

    @staticmethod
    def _children(widget):
        child = widget.get_first_child()
        while child is not None:
            if child.should_layout():
                yield child
            child = child.get_next_sibling()

    def _lines(self, widget, width):
        """Split the children into lines no wider than `width` (None: one line).

        Returns [(height, [(child, x, child_width, child_height), ...]), ...].
        """
        lines, line, x, line_h = [], [], 0, 0
        for child in self._children(widget):
            cmin, cnat, _, _ = child.measure(Gtk.Orientation.HORIZONTAL, -1)
            w = cnat if width is None else max(cmin, min(cnat, width))
            if line and width is not None and x + self.child_spacing + w > width:
                lines.append((line_h, line))
                line, x, line_h = [], 0, 0
            if line:
                x += self.child_spacing
            _, h, _, _ = child.measure(Gtk.Orientation.VERTICAL, w)
            line.append((child, x, w, h))
            x += w
            line_h = max(line_h, h)
        if line:
            lines.append((line_h, line))
        return lines

    def do_get_request_mode(self, widget):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, widget, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            minimum = natural = 0
            for child in self._children(widget):
                cmin, cnat, _, _ = child.measure(orientation, -1)
                minimum = max(minimum, cmin)
                natural += cnat + (self.child_spacing if natural else 0)
            return minimum, natural, -1, -1
        lines = self._lines(widget, for_size if for_size >= 0 else None)
        height = sum(h for h, _ in lines) + self.line_spacing * max(0, len(lines) - 1)
        return height, height, -1, -1

    def do_allocate(self, widget, width, height, baseline):
        rtl = widget.get_direction() == Gtk.TextDirection.RTL
        y = 0
        for line_h, line in self._lines(widget, width):
            for child, x, w, h in line:
                rect = Gdk.Rectangle()
                rect.x = width - x - w if rtl else x
                rect.y = y + (line_h - h) // 2
                rect.width, rect.height = w, h
                child.size_allocate(rect, -1)
            y += line_h + self.line_spacing


class WrapBox(Gtk.Widget):
    """The parts of the Adw.WrapBox API Procular uses: append() and remove()."""

    def __init__(self, child_spacing=0, line_spacing=0, **kwargs):
        super().__init__(**kwargs)
        self.set_layout_manager(WrapLayout(child_spacing, line_spacing))

    def append(self, child):
        child.set_parent(self)

    def remove(self, child):
        if child.get_parent() is self:
            child.unparent()

    def _release_children(self):
        # Gtk.Widget does not release children a subclass added itself
        while (child := self.get_first_child()) is not None:
            child.unparent()

    def teardown(self):
        """Called by lifecycle.destroy_tree() when the window closes.

        do_dispose() alone is not enough: PyGObject 3.48 (Ubuntu 24.04) skips Python dispose
        overrides of widgets whose Python wrapper is already gone, and GTK then warns
        "Finalizing WrapBox, but it still has children left".
        """
        self._release_children()

    def do_dispose(self):
        self._release_children()
        Gtk.Widget.do_dispose(self)
