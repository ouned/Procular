"""Header bar button that explains the less obvious values of the visible page."""

from gi.repository import Gdk, Gtk

from ..providers.processes import NCPU
from ..util.lifecycle import own_method


def _cpu_scale():
    if NCPU == 1:
        return "100 means the processor is fully busy."
    return (f"100 means all {NCPU} processor cores are fully busy, so a program that keeps one core "
            f"busy shows {100 / NCPU:.3g}.")




# legends: small samples of what the explained colours look like on screen
HEAT = ("heat", None)  # the five heat steps of a resource cell, low to high
ROW_STATES = ("rows", None)  # the state tints a whole process row gets, in their configured colours
SERVICE_DOTS = ("dots", [("running", "Running"), ("active", "Active, idle"), ("changing", "Starting, stopping"),
                         ("failed", "Failed"), ("inactive", "Inactive")])
DEVICE_DOTS = ("dots", [("running", "Active"), ("active", "Suspended"), ("changing", "No driver")])
SOCKET_DOTS = ("dots", [("running", "Connected"), ("active", "Listening"), ("changing", "Opening, closing"),
                        ("inactive", "Not connected")])
ROW_STATE_LABELS = {"new": "Started", "term": "Exited", "stopped": "Suspended", "zombie": "Zombie"}

# page -> (title, [(term, summary, details, legend), ...]). The summary is the one line most
# readers need; details and legend are optional.
HELP = {
    "processes": ("Reading the Process List", [
        ("CPU",
         "Share of the whole machine, averaged since the last update.",
         f"{_cpu_scale()} An empty cell means idle.", None),
        ("Kernel CPU, CPU time",
         "Time spent inside the kernel, and all processor time used so far.",
         "Kernel CPU is the part of CPU the kernel spends on the process's behalf. CPU time counts "
         "since the process started (hours:minutes:seconds); with several busy threads it grows "
         "faster than the clock.", None),
        ("CPU and memory history",
         "The last 60 updates, newest on the right. Hover for the values.",
         "Every graph has its own scale, so compare the numbers, not the heights: CPU goes from zero "
         "to the row's own peak (at least one core), memory is zoomed in on its recent range to show "
         "the trend.", None),
        ("Private bytes, Working set",
         "Private bytes is what a process really costs.",
         "It is the memory only this process uses. Working set is everything it has in RAM, "
         "including libraries and files shared with other processes, so adding working sets up "
         "counts shared memory several times. Virtual size is reserved address space, most of it "
         "not real memory.", None),
        ("GPU, GPU memory",
         "How busy the process kept its busiest GPU engine, and what its GPU buffers occupy.",
         "The engines are graphics, compute, video and copy; 100 means one was fully busy since the "
         "last update. GPU memory counts buffers in the GPU's own memory and in system RAM. Both come "
         "from the kernel's DRM statistics (for NVIDIA from the driver's NVML library); other users' "
         "processes are only visible to administrators.", None),
        ("I/O rate, Disk rate",
         "I/O is everything read and written, Disk only what reaches a storage device.",
         "I/O covers files, pipes and terminals, also when the cache serves it.", None),
        ("OOM score, OOM adjustment",
         "Which process the kernel ends first when memory runs out: the highest score.",
         "The score grows with the memory a process uses; the adjustment (−1000 to 1000) is added to it, "
         "and −1000 exempts the process. Change it with OOM Killer in the context menu; lowering it below 0 "
         "usually needs administrator rights.", None),
        ("n/a",
         "The value exists but may not be read.",
         "The I/O counters of other users' processes are only available to administrators.", None),
        ("Cell backgrounds",
         "Orange to red marks high values.",
         "CPU from 2% of one core to a full core, memory from 2% to 30% of the RAM, I/O from "
         "16 KiB/s to 128 MiB/s. Heat colouring can be switched off in the preferences.", HEAT),
        ("Row colours",
         "Rows are coloured by user or by category; the gear button shows which.",
         "In every mode, these states tint the whole row: started and exited processes for a moment, "
         "suspended and zombie processes for as long as they last.", ROW_STATES),
        ("Sorting in tree view",
         "Each branch is ordered by its busiest member.",
         "Sort by a resource column and follow the first rows down to reach the top consumer.", None),
        ("Status bar",
         "Physical memory is memory in use without the file cache.",
         "The cache is given back when programs need the memory, so it does not count as used.", None),
    ]),
    "services": ("Reading the Services List", [
        ("CPU",
         "All of the unit's processes together, in percent of the whole machine.",
         _cpu_scale(), None),
        ("Status dot",
         "Whether the unit is running.",
         "Running includes listening and waiting units. Active, idle means nothing is running, "
         "such as a finished one-off job.", SERVICE_DOTS),
        ("Status, State, Startup",
         "systemd's overall state, its detail, and whether the unit starts on its own.",
         "State is the detail (running, exited, dead, …). Startup is enabled, disabled, or static "
         "(cannot be enabled, other units start it).", None),
        ("User, PID, Processes",
         "Taken from the unit's processes; empty when nothing is running.",
         "PID is the lowest one, User the user that process runs as.", None),
        ("Memory",
         "The working sets of the unit's processes added up.",
         "Memory they share is counted once per process, so the real cost is usually lower.", None),
        ("Pressure",
         "How much of the last 10 seconds the unit's processes spent waiting for CPU, memory or I/O.",
         "The cell names the resource they waited for most; hover it for all three, also over 1 and 5 "
         "minutes. “All” means every one of its processes was waiting at once. It is empty when nothing "
         "waited, or when the kernel does not report pressure (PSI).", None),
        ("Row colours",
         "When rows are coloured by user (gear button), a unit has the colour of its user.",
         "Units with nothing running have none. Bold names are running, red ones failed.", None),
        ("Scope",
         "System units belong to the machine, user units to your login session.", None, None),
    ]),
    "network": ("Reading the Network List", [
        ("Sockets of other users",
         "Only your own processes' sockets show who owns them.",
         "The banner offers to resolve the others as administrator.", None),
        ("Remote host",
         "The name is looked up from the address by asking your DNS server.",
         "Switch “Resolve host names” off to avoid that.", None),
        ("State",
         "The dot before each socket shows its state; hover it for the exact TCP state.",
         "Listening sockets wait for incoming connections and have no remote address.", SOCKET_DOTS),
    ]),
    "disk": ("Reading the Disk Page", [
        ("Processes",
         "Processes that used a storage device in the last 30 seconds.",
         "Rates count only what reaches the device, not what the cache served. Totals are since "
         "the process started.", None),
        ("Missing processes",
         "Other users' activity shows under the devices, not in the process list.",
         "Disk counters can only be read for your own processes. Which files a process reads or "
         "writes would need eBPF and is not shown.", None),
        ("Active time",
         "How much of the time a device was busy with requests.",
         "Near 100% the device is the bottleneck, whatever the transfer rate.", None),
    ]),
    "devices": ("Reading the Device Tree", [
        ("Tree",
         "Each device is shown where it is connected.",
         "A USB device sits below the hub port it is plugged into, the hub below its controller, the "
         "controller below the PCI bridge it hangs off. Devices the firmware describes (buttons, "
         "battery, embedded controller) are platform devices.", None),
        ("Status",
         "Whether a driver runs the device, and whether it is in power saving now.",
         "Suspended devices were put to sleep by runtime power management until they are needed. "
         "A device without a driver does nothing; some functions, such as host bridges, need none and "
         "have no status.", DEVICE_DOTS),
        ("Link",
         "The speed of the device's connection: PCIe generation and lanes, or USB speed.",
         "“(of …)” means the link runs below what both ends can do. Graphics cards and Wi-Fi do that "
         "to save power while idle; a busy device should not. The link of a suspended device is "
         "not read, since reading it wakes the device: it keeps the value last read.", None),
        ("Time suspended",
         "How much of the time the kernel kept the device asleep, since it appeared.",
         "A device that is rarely used but never suspended is a common cause of a short battery life. "
         "“Always on” means runtime power management is switched off for it. NVMe drives and Wi-Fi adapters "
         "also save power on their own, which is not counted here.", None),
        ("Power",
         "Shown only where the hardware measures it: graphics, batteries, some power supplies.",
         "Most devices have no power meter. On a processor with integrated graphics, the graphics "
         "reading (PPT) is the whole chip. The battery's reading is the whole computer's draw, shown in "
         "the status line too. A device that may suspend has its sensor read less often, since reading it "
         "keeps it awake.", None),
        ("Interrupts/s",
         "How often the device asked a processor for attention, for PCI devices.", None, None),
        ("Interfaces",
         "What the driver made of the device: network interfaces, disks, sound cards, input devices.",
         "Hover for the device files in /dev that belong to it.", None),
        ("Used by",
         "Processes that hold one of the device's files open.",
         "Programs that reach a device through a service show the service: sound goes through "
         "PipeWire, input through the compositor. Other users' processes are only checked as "
         "administrator.", None),
        ("Show all devices",
         "Everything in the kernel's device tree.",
         "Also virtual devices (loop devices, terminals), the firmware's ACPI objects, processors, "
         "memory blocks, USB interfaces and the services of PCIe ports.", None),
    ]),
    "history": ("Reading the History", [
        ("What is recorded",
         "Processes that exited while Procular was running.",
         "Kernel threads are left out unless they are shown in the process list.", None),
        ("Exit status",
         "Red rows exited with an error or were killed.",
         "The status is only known for your own processes, and only when Procular caught the "
         "process between exiting and being cleaned up by its parent.", None),
        ("Peak CPU",
         "The highest value seen at an update, in percent of the whole machine.",
         f"{_cpu_scale()} Bursts between two updates can be missed.", None),
    ]),
}


class _RowSwatch(Gtk.DrawingArea):
    """A tiny table row in a highlight colour: tinted background and the stripe at its start."""

    def __init__(self, color, alpha):
        super().__init__(content_width=28, content_height=14, valign=Gtk.Align.CENTER)
        self._rgba = Gdk.RGBA()
        self._rgba.parse(color)
        self._alpha = alpha
        self.set_draw_func(own_method("_draw"))

    def _draw(self, _area, cr, width, height):
        c = self._rgba
        cr.set_source_rgba(c.red, c.green, c.blue, self._alpha)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.set_source_rgba(c.red, c.green, c.blue, 0.9)
        cr.rectangle(0, 0, 3, height)
        cr.fill()


def legend_item(sample, text):
    item = Gtk.Box(spacing=6)
    item.append(sample)
    item.append(Gtk.Label(label=text, css_classes=["info-legend-label"]))
    return item


class InfoButton(Gtk.MenuButton):
    def __init__(self, win):
        super().__init__(icon_name="help-about-symbolic", tooltip_text="What the Values Mean (F1)")
        self.win = win
        self.config = win.config
        self.title = Gtk.Label(xalign=0, css_classes=["title-4"])
        self.entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["info-menu"])
        box.append(self.title)
        box.append(Gtk.Label(label="F1 opens these explanations on every page.", xalign=0,
                             css_classes=["info-subtitle"]))
        box.append(self.entries)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                           max_content_height=600)
        self.scroller.set_child(box)
        # fades the bottom edge while there is more to scroll to
        self.fade = Gtk.Box(css_classes=["info-fade"], valign=Gtk.Align.END, can_target=False)
        adj = self.scroller.get_vadjustment()
        for signal in ("value-changed", "changed"):
            adj.connect(signal, self._update_fade)
        overlay = Gtk.Overlay(child=self.scroller)
        overlay.add_overlay(self.fade)
        self.popover = Gtk.Popover()
        self.popover.set_child(overlay)
        self.popover.connect("show", lambda _p: self._fill())
        self.set_popover(self.popover)

    def _update_fade(self, adj):
        self.fade.set_visible(adj.get_value() < adj.get_upper() - adj.get_page_size() - 1)

    def _fill(self):
        """Show the explanations for the page that is visible now (rebuilt every time: the
        legends show the colours as they are configured now)."""
        page = self.win.stack.get_visible_child_name()
        if page not in HELP:
            return
        title, entries = HELP[page]
        self.title.set_text(title)
        child = self.entries.get_first_child()
        while child is not None:
            self.entries.remove(child)
            child = self.entries.get_first_child()
        for i, (term, summary, details, legend) in enumerate(entries):
            if i:
                self.entries.append(Gtk.Separator())
            entry = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            entry.append(Gtk.Label(label=term, xalign=0, css_classes=["info-term"]))
            entry.append(self._text(summary, "info-summary"))
            if legend is not None:
                entry.append(self._legend(*legend))
            if details:
                entry.append(self._text(details, "info-text"))
            self.entries.append(entry)
        self.scroller.get_vadjustment().set_value(0)

    @staticmethod
    def _text(text, css):
        return Gtk.Label(label=text, xalign=0, wrap=True, width_chars=46, max_width_chars=46, css_classes=[css])

    def _legend(self, kind, items):
        if kind == "heat":
            box = Gtk.Box(spacing=6, css_classes=["info-legend"])
            box.append(Gtk.Label(label="low", css_classes=["info-legend-label"]))
            for step in range(1, 6):
                box.append(Gtk.Box(css_classes=["info-swatch", f"heat-{step}"], valign=Gtk.Align.CENTER))
            box.append(Gtk.Label(label="high", css_classes=["info-legend-label"]))
            return box
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                           column_spacing=14, row_spacing=4, css_classes=["info-legend"], can_target=False)
        if kind == "dots":
            for state, text in items:
                dot = Gtk.Box(css_classes=["status-dot", f"status-{state}"], valign=Gtk.Align.CENTER)
                flow.append(legend_item(dot, text))
        else:  # row states, as configured: disabled ones do not colour anything
            highlights = self.config.get("highlights")
            alpha = self.config.get("highlight_alpha")
            flow.set_max_children_per_line(2)  # four of them: 2 x 2 rather than 3 + 1
            for key, text in ROW_STATE_LABELS.items():
                if highlights[key]["enabled"]:
                    flow.append(legend_item(_RowSwatch(highlights[key]["color"], alpha), text))
        return flow
