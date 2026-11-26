"""Deterministic teardown of closed windows.

GTK 4 has no gtk_widget_destroy(): closing a window only drops the toolkit's reference to it.
The widgets are freed once nothing else references them, and in a PyGObject application
something almost always does. A callback connected to a child widget that uses its parent
(``button.connect("clicked", self.on_clicked)``), or a child keeping a ``self.win`` back
reference, forms a cycle that runs through C: parent -> child widget -> callback -> parent
wrapper. Python's garbage collector cannot follow the C half, so the cycle is never
collected and every closed window stays in memory with all its widgets and data.

destroy_tree() breaks those cycles explicitly, the way gtk_widget_destroy() did in GTK 3.
"""

import os
import weakref

from gi.repository import Adw, GLib

_DEBUG = bool(os.environ.get("PROCULAR_DEBUG_LIFECYCLE"))


def weak_method(method):
    """A callback that calls the bound `method` without keeping its object alive.

    For callbacks a widget registers on itself through C (draw functions, event controllers):
    with a strong reference the widget could never be freed.
    """
    ref = weakref.WeakMethod(method)

    def call(*args):
        func = ref()
        return func(*args) if func is not None else None

    return call


def own_method(name):
    """A callback for a widget's own signal or draw function that calls the widget's method
    `name`, found on the widget GTK passes as the first argument: nothing refers to the object.

    Use it instead of weak_method() for widgets that nothing in Python holds on to, such as list
    cells: PyGObject drops the wrapper of such a widget and makes a new one when it is needed
    again, and a weak reference to the first wrapper is dead although the widget lives.
    """
    def call(widget, *args):
        return getattr(widget, name)(widget, *args)

    return call


def is_destroyed(widget):
    """True once destroy_tree() has run on the window `widget` belonged to."""
    return getattr(widget, "_destroyed", False)


def destroy_tree(root):
    """Free a closed window (or any unparented widget) and everything below it.

    Widgets that carry Python state ("owned": our subclasses and widgets we attached attributes
    to) are what keeps the tree alive, so they get the treatment:

    1. their optional ``teardown()`` method runs (release models, popovers, timers, ...),
    2. their attributes are dropped, which releases the widgets and objects they referenced,
    3. they are disposed from the root down. Disposing a widget makes it release its children;
       plain GTK children are freed on the spot (and with them the callbacks connected to
       them), owned children are disposed next.

    Afterwards only empty shells remain, and those go away with their last Python reference;
    nothing is left for the garbage collector. Callbacks that may still fire later (worker
    results, pkexec) must check is_destroyed().
    """
    owned = []
    stack = [root]
    while stack:
        widget = stack.pop()
        teardown = getattr(widget, "teardown", None)
        if callable(teardown):
            teardown()
        if getattr(widget, "__dict__", None):
            owned.append(widget)
        children = []
        child = widget.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        stack.extend(reversed(children))  # pre-order: parents before their children

    for widget in owned:
        widget.__dict__.clear()
        widget._destroyed = True
    root.run_dispose()
    for widget in owned:
        if widget is root:
            continue
        if widget.get_parent() is None:
            widget.run_dispose()
        elif _DEBUG:
            # kept by a parent that something else still references: it is released with that parent
            print(f"lifecycle: {type(widget).__name__} still inside {type(widget.get_parent()).__name__}")
    if _DEBUG:
        print(f"lifecycle: destroyed {type(root).__name__} ({len(owned)} owned widgets)")


def destroy_on_close(window):
    """Free `window` (a Gtk.Window or an Adw.Dialog) and its widgets once it has been closed.

    Every secondary window and dialog that keeps Python state needs this; the main window lives
    as long as the application.
    """

    def on_close(win):
        # from idle: after the default handler has closed the window / the dialog left its host
        GLib.idle_add(_destroy_closed, win)
        return False

    window.connect("closed" if isinstance(window, Adw.Dialog) else "close-request", on_close)


def _destroy_closed(window, tries=40):
    if isinstance(window, Adw.Dialog):
        # a closed dialog stays in its window until the closing animation has finished
        if window.get_parent() is not None:
            if tries > 0:
                GLib.timeout_add(250, _destroy_closed, window, tries - 1)
            return False
    elif window.get_visible():
        return False  # still shown: another close-request handler vetoed the close
    destroy_tree(window)
    return False
