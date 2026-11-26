"""Process manipulation with automatic pkexec fallback."""

import os
import signal

import psutil
from gi.repository import Adw, Gdk, Gio, Gtk

from ..helper.procular_helper import (OOM_SCORE_ADJ_MAX, OOM_SCORE_ADJ_MIN, for_each_thread, online_cpus,
                                      read_starttime, set_nice, set_oom_score_adj, signal_if_same)
from ..helper.procular_helper import set_affinity as set_thread_affinity
from ..util import privileged
from ..util.lifecycle import destroy_on_close, is_destroyed

SIGNAL_NAMES = {s.value: s.name for s in signal.Signals}

IOPRIO_CLASSES = {1: "Realtime", 2: "Best effort", 3: "Idle"}


def _report(win, ok_msg):
    def cb(_result, error):
        if is_destroyed(win):
            return  # e.g. a properties window closed while the pkexec dialog was open
        if error:
            win.toast(f"Failed: {error}")
        elif ok_msg:
            win.toast(ok_msg)
    return cb


def targets_of(items):
    """Snapshot the identity of each selected process for a later action."""
    return tuple((item.pid, item.rec["starttime"]) for item in items)


def _missing(win, count):
    win.toast("Process no longer exists" if count == 1 else "The processes no longer exist")


def send_signal(win, items, sig):
    """Send sig to the processes `items` (ProcessItems), and only to those very processes.

    A process that exited is skipped, also when its pid already belongs to another process:
    items can be from the last scan, or from a confirmation dialog that stayed open a while."""
    denied = []
    sent = 0
    failed = None
    for pid, starttime in targets_of(items):
        try:
            sent += signal_if_same(pid, starttime, sig)
        except PermissionError:
            denied.append((pid, starttime))
        except OSError as err:
            failed = err
    if failed is not None:
        win.toast(f"Failed: {failed}")
    if not sent and not denied:
        if failed is None:
            _missing(win, len(items))
        return
    count = sent + len(denied)
    name = SIGNAL_NAMES.get(sig, sig)
    ok_msg = f"Sent {name} to {count} processes" if count > 1 else None
    if denied:
        privileged.run_for_processes("signal", denied, [sig], _report(win, ok_msg))
    elif ok_msg:
        win.toast(ok_msg)


def names_summary(items, limit=4):
    names = [i.name for i in items[:limit]]
    if len(items) > limit:
        return ", ".join(names) + f" and {len(items) - limit} more"
    return ", ".join(names[:-1]) + " and " + names[-1]


def confirm_signal(win, items, sig, verb, roots=None):
    """Ask before terminating/killing (configurable, except for several selected processes).

    items: every process that receives the signal; roots: the processes the user picked
    (default: just items[0] — the others are its descendants)."""
    roots = roots or items[:1]
    # Always ask when several processes were picked: with Ctrl+A, Delete would otherwise end
    # the whole desktop session without a word.
    if not win.config.get("confirm_kill") and len(roots) == 1:
        send_signal(win, items, sig)
        return
    sig_name = SIGNAL_NAMES.get(sig, str(sig))
    if len(items) == 1:
        heading = f"{verb} {items[0].name}?"
        body = f"Process {items[0].pid} will receive {sig_name}."
    elif len(roots) == 1:
        heading = f"{verb} {items[0].name} and {len(items) - 1} descendant processes?"
        body = f"All processes in the tree will receive {sig_name}."
    elif len(roots) == len(items):
        heading = f"{verb} {len(items)} processes?"
        body = f"{names_summary(items)} will receive {sig_name}."
    else:
        heading = f"{verb} {len(roots)} processes and their descendants?"
        body = f"{names_summary(roots)} and all their descendants ({len(items)} processes) will receive {sig_name}."
    body += " Unsaved data may be lost."
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", verb)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "ok":
            send_signal(win, items, sig)

    dialog.connect("response", on_response)
    dialog.present(win)


def _apply(win, targets, func, op, args, msg):
    """Apply func(pid, starttime); denied targets go to the helper with the same identity.

    The values set here belong to threads on Linux: func has to change every thread of the
    process (see for_each_thread in the helper)."""
    denied = []
    failed = None
    applied = False
    for pid, starttime in targets:
        try:
            func(pid, starttime)
            applied = True
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied):
            denied.append((pid, starttime))
        except (OSError, psutil.Error) as err:  # e.g. a kernel thread bound to its CPU
            failed = err
    if failed is not None:
        win.toast(f"Failed: {failed}")
    if denied:
        privileged.run_for_processes(op, denied, args, _report(win, msg))
    elif failed is None:
        if applied:
            win.toast(msg)
        else:
            _missing(win, len(targets))


def renice(win, targets, value):
    """Set the nice value of the selected processes (of all their threads)."""
    subject = str(targets[0][0]) if len(targets) == 1 else f"{len(targets)} processes"
    _apply(win, targets, lambda pid, start: set_nice(pid, value, start), "renice", [value],
           f"Priority of {subject} set to {value}")


def set_ionice(win, targets, klass, level):
    msg = f"I/O priority set to {IOPRIO_CLASSES[klass]}"
    if len(targets) > 1:
        msg += f" for {len(targets)} processes"

    def set_thread(tid):
        try:
            thread = psutil.Process(tid)  # psutil takes a thread id for a process id here
            if klass == 3:
                thread.ionice(psutil.IOPRIO_CLASS_IDLE)
            else:
                thread.ionice({1: psutil.IOPRIO_CLASS_RT, 2: psutil.IOPRIO_CLASS_BE}[klass], level)
        except psutil.NoSuchProcess:
            raise ProcessLookupError(tid) from None  # the thread has ended: for_each_thread goes on

    _apply(win, targets, lambda pid, start: for_each_thread(pid, set_thread, start), "ionice", [klass, level], msg)


def get_ionice(pid, nice):
    """Current I/O priority as "class:level" (the format of the proc.ionice action), or ""."""
    try:
        io = psutil.Process(pid).ionice()
    except (psutil.Error, OSError):
        return ""
    klass, level = int(io.ioclass), io.value
    if klass == 0:  # no explicit class: the kernel uses best effort with a level derived from nice
        klass = 2
        level = (nice + 20) // 5 if nice is not None else 4
    elif klass == 3:
        level = 0
    return f"{klass}:{level}"


def set_oom_adj(win, targets, value):
    """Set the OOM score adjustment of the selected processes (it belongs to the whole process)."""
    subject = str(targets[0][0]) if len(targets) == 1 else f"{len(targets)} processes"
    _apply(win, targets, lambda pid, start: set_oom_score_adj(pid, value, start), "oom-adj", [value],
           f"OOM score adjustment of {subject} set to {value}")


def get_oom_adj(pid):
    """Current OOM score adjustment, or None. (One small read, when a menu opens.)"""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "rb") as fh:
            return int(fh.read())
    except (OSError, ValueError):
        return None


def get_affinity(pid):
    try:
        return set(os.sched_getaffinity(pid))
    except OSError:
        return None


def set_affinity(win, targets, cpus):
    msg = "Affinity updated" + ("" if len(targets) == 1 else f" for {len(targets)} processes")
    _apply(win, targets, lambda pid, start: set_thread_affinity(pid, cpus, start), "affinity",
           [",".join(str(c) for c in sorted(cpus))], msg)


def _dialog_title(what, targets, name):
    return f"{what} — {name} ({targets[0][0]})" if len(targets) == 1 else f"{what} — {len(targets)} processes"


def open_location(win, path):
    if not path:
        win.toast("Executable path not available")
        return
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path.replace(" (deleted)", "")))
    launcher.open_containing_folder(win, None, None)


def copy_text(text):
    Gdk.Display.get_default().get_clipboard().set(text)


class AffinityDialog(Adw.Dialog):
    """The checks start out as the first process's affinity."""

    def __init__(self, win, targets, name):
        super().__init__(title=_dialog_title("Affinity", targets, name))
        destroy_on_close(self)
        self.win = win
        self.targets = targets
        pid, starttime = targets[0]
        current = (get_affinity(pid) if read_starttime(pid) == starttime else None) or set()
        cpus = sorted(online_cpus() | current)
        self.set_content_width(420)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(Gtk.Label(label="Allow the process to run on these CPUs:" if len(targets) == 1
                             else f"Allow the {len(targets)} processes to run on these CPUs:", xalign=0))
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                           column_spacing=6, row_spacing=6, homogeneous=True)
        self.checks = []
        for cpu in cpus:
            check = Gtk.CheckButton(label=f"CPU {cpu}", active=(cpu in current))
            self.checks.append((cpu, check))
            grid.append(check)
        box.append(grid)

        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        all_btn = Gtk.Button(label="Select All")
        all_btn.connect("clicked", lambda *_: [c.set_active(True) for _, c in self.checks])
        ok = Gtk.Button(label="Apply")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._apply)
        buttons.append(all_btn)
        buttons.append(ok)
        box.append(buttons)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _apply(self, *_):
        cpus = {cpu for cpu, c in self.checks if c.get_active()}
        if not cpus:
            self.win.toast("Select at least one CPU")
            return
        set_affinity(self.win, self.targets, cpus)
        self.close()


class NiceDialog(Adw.Dialog):
    def __init__(self, win, targets, name, current):
        super().__init__(title=_dialog_title("Priority", targets, name))
        destroy_on_close(self)
        self.win = win
        self.targets = targets
        self.set_content_width(360)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        group = Adw.PreferencesGroup(margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                                     description="Lower values mean higher priority. "
                                                 "Values below 0 need administrator rights.")
        self.spin = Adw.SpinRow.new_with_range(-20, 19, 1)
        self.spin.set_title("Nice value")
        self.spin.set_value(current or 0)
        group.add(self.spin)
        btn = Gtk.Button(label="Apply", halign=Gtk.Align.END, margin_top=12)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._apply)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(btn)
        btn.set_margin_end(12)
        btn.set_margin_bottom(12)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _apply(self, *_):
        renice(self.win, self.targets, int(self.spin.get_value()))
        self.close()


class OomDialog(Adw.Dialog):
    def __init__(self, win, targets, name, current):
        super().__init__(title=_dialog_title("OOM Killer", targets, name))
        destroy_on_close(self)
        self.win = win
        self.targets = targets
        self.set_content_width(400)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        group = Adw.PreferencesGroup(
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
            description="When memory runs out, the kernel ends the process with the highest OOM score. "
                        "The adjustment is added to it: higher values make the process the first to go, "
                        "−1000 exempts it. Lowering it below 0 usually needs administrator rights.")
        self.spin = Adw.SpinRow.new_with_range(OOM_SCORE_ADJ_MIN, OOM_SCORE_ADJ_MAX, 50)
        self.spin.set_title("OOM score adjustment")
        self.spin.set_value(current or 0)
        group.add(self.spin)
        btn = Gtk.Button(label="Apply", halign=Gtk.Align.END, margin_top=12, margin_end=12, margin_bottom=12)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._apply)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(btn)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _apply(self, *_):
        set_oom_adj(self.win, self.targets, int(self.spin.get_value()))
        self.close()
