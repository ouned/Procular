"""About dialog: the animated logo, what Procular is, a thank-you to System Informer, and a tune."""

import sys
import time

from gi.repository import Adw, GLib, Gtk

from ..config import APP_NAME, VERSION
from ..util import chiptune
from ..util.audio import LoopPlayer
from ..util.lifecycle import destroy_on_close, is_destroyed, weak_method
from ..widgets.logo_animation import AnimatedLogo

SYSTEM_INFORMER_URL = "https://systeminformer.sourceforge.io/"
# from handing the samples to the player until they are heard, roughly: the logo waits that long
OUTPUT_DELAY = 0.12
# opening the dialog brings the music in this softly instead of at full volume at once
FADE_IN_SECONDS = 1.5
VOLUME_ICONS = ((0.34, "audio-volume-low-symbolic"), (0.67, "audio-volume-medium-symbolic"),
                (1.0, "audio-volume-high-symbolic"))

DESCRIPTION = (
    "A process manager for administrators and developers on GNOME. It shows what is running on this computer "
    "as a live process tree, with services, network connections, disk activity and hardware "
    "graphs, and lets you inspect, suspend, reprioritise or end any process."
)
TRIBUTE = (
    "Procular exists because of "
    f'<a href="{SYSTEM_INFORMER_URL}">System Informer</a>, formerly Process Hacker: the Windows tool '
    "created by wj32 and developed by dmex and many contributors. Its process tree, row colours, "
    "graphs and properties pages are the model for this application. Thank you for the inspiration!"
)
DISCLAIMER = "Procular is an independent project and not affiliated with System Informer."


def _label(text, *css_classes, markup=False):
    label = Gtk.Label(label=text, use_markup=markup, wrap=True, justify=Gtk.Justification.CENTER,
                      max_width_chars=54)
    for css_class in css_classes:
        label.add_css_class(css_class)
    return label


class AboutDialog(Adw.Dialog):
    def __init__(self, app):
        super().__init__(title=f"About {APP_NAME}", content_width=440)
        destroy_on_close(self)
        self.config = app.config

        self._track_name = self.config.get("about_track")
        if self._track_name not in chiptune.TRACKS:
            self._track_name = chiptune.DEFAULT_TRACK
        self.track = chiptune.TRACKS[self._track_name]
        self.logo = AnimatedLogo(self.track.cues())
        self.player = self._make_player()
        self._fade = 1.0  # 0..1 of the slider's volume, while the music fades in
        self._fade_start = None  # monotonic seconds; None: no fade in waiting or running
        self._music_offset = 0.0  # seconds into the tune at which the player was started

        self.pause_button = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text="Pause")
        self.pause_button.connect("toggled", self._on_pause_toggled)

        self.music_button = Gtk.ToggleButton(active=self.config.get("about_music"))
        self.music_button.connect("toggled", self._on_music_toggled)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self.volume.set_draw_value(False)
        self.volume.set_size_request(120, -1)
        self.volume.set_tooltip_text("Volume")
        self.volume.set_value(min(max(self.config.get("about_volume"), 0.0), 1.0))
        self.volume.connect("value-changed", self._on_volume_changed)
        self.track_choice = Gtk.DropDown.new_from_strings([track.title for track in chiptune.TRACKS.values()])
        self.track_choice.set_tooltip_text("Tune")
        self.track_choice.set_selected(list(chiptune.TRACKS).index(self._track_name))
        self.track_choice.connect("notify::selected", self._on_track_chosen)
        self._apply_volume()
        self._show_music_state()
        header = Adw.HeaderBar(show_title=False)
        header.pack_start(self.pause_button)
        header.pack_start(self.music_button)
        header.pack_start(self.volume)
        header.pack_end(self.track_choice)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=24, margin_end=24,
                      margin_bottom=20)
        box.append(self.logo)
        box.append(_label(APP_NAME, "title-1"))
        version = Gtk.Label(label=f"Version {VERSION}", selectable=True, halign=Gtk.Align.CENTER)
        version.add_css_class("badge-pill")
        version.add_css_class("numeric")
        box.append(version)
        box.append(_label(DESCRIPTION, "about-text"))

        tribute = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tribute.add_css_class("card")
        tribute.add_css_class("about-tribute")
        tribute.append(_label("In Honour of System Informer", "heading"))
        tribute.append(_label(TRIBUTE, markup=True))
        tribute.append(_label(DISCLAIMER, "about-footer", "dim"))
        box.append(tribute)

        versions = "  ·  ".join((
            "MIT License",
            f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ))
        footer = _label(f"{versions}\n♪ 8-bit theme, synthesised by {APP_NAME} as it plays", "about-footer", "dim")
        footer.set_selectable(True)
        box.append(footer)

        # the content can be taller than a small main window
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        scroller.set_child(box)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scroller)
        self.set_child(toolbar)
        self.set_focus(self.music_button)  # not the first link in the text

        self.connect("closed", lambda *_: self.player.stop())  # at once, not after the closing animation
        if self.music_button.get_active():
            # silent until the player has started: the fade then runs while the music is heard
            self._fade, self._fade_start = 0.0, 0.0
            self._apply_volume()
            self._start_music(0.0)

    def teardown(self):
        self.player.stop()

    # ------------------------------------------------------------------ music
    def _make_player(self, volume=1.0):
        return LoopPlayer(self.track.chunks, chiptune.RATE, volume=volume, on_started=self._on_music_started,
                          on_unavailable=self._on_music_unavailable)

    def _music_wanted(self):
        return (self.music_button.get_active() and self.music_button.get_sensitive()
                and not self.pause_button.get_active())

    def _start_music(self, offset=None):
        """Play from `offset` seconds into the tune, by default from where the logo is.

        The logo stands still there until the music is heard, then they go on together.
        """
        if self.player.playing:
            return
        self._music_offset = self.logo.position() if offset is None else offset
        self.logo.hold(self._music_offset)
        self.player.start(round(self._music_offset * chiptune.RATE))

    def _on_pause_toggled(self, button):
        paused = button.get_active()
        button.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        button.set_tooltip_text("Play" if paused else "Pause")
        if paused:
            self.player.stop()
            self.logo.hold(self.logo.position())
        elif self._music_wanted():
            self._start_music()
        else:
            self.logo.go_on()

    def _on_track_chosen(self, dropdown, _pspec):
        self._track_name = list(chiptune.TRACKS)[dropdown.get_selected()]
        self.config.set("about_track", self._track_name)
        self.track = chiptune.TRACKS[self._track_name]
        self.player.stop()
        self.player = self._make_player(self.player.volume)
        self.logo.set_cues(self.track.cues())
        # the new tune from its start
        if self.pause_button.get_active():
            self.logo.hold(0.0)
        elif self._music_wanted():
            self._start_music(0.0)
        else:
            self.logo.sync(None)

    def _show_music_state(self):
        on = self.music_button.get_active()
        position = self.volume.get_value()
        icon = next(name for limit, name in VOLUME_ICONS if position <= limit)
        self.music_button.set_icon_name(icon if on and position > 0 else "audio-volume-muted-symbolic")
        self.music_button.set_tooltip_text("Music On" if on else "Music Off")
        self.volume.set_sensitive(on and self.music_button.get_sensitive())

    def _apply_volume(self):
        # squared: equal steps of the slider then sound like equal steps of loudness
        self.player.volume = (self.volume.get_value() * self._fade) ** 2

    def _on_volume_changed(self, scale):
        self.config.set("about_volume", round(scale.get_value(), 3))
        self._apply_volume()
        self._show_music_state()

    def _on_music_toggled(self, button):
        self.config.set("about_music", button.get_active())
        self._show_music_state()
        if self._music_wanted():
            self._start_music()
        elif not button.get_active():
            self.player.stop()
            if not self.pause_button.get_active():
                self.logo.go_on()  # in case it was waiting for the music to be heard

    def _on_music_started(self, when):
        if is_destroyed(self):
            return
        self.logo.sync(when + OUTPUT_DELAY - self._music_offset)
        if self._fade_start == 0.0:
            self._fade_start = time.monotonic()
            GLib.timeout_add(30, weak_method(self._fade_step))

    def _fade_step(self):
        if is_destroyed(self):
            return GLib.SOURCE_REMOVE
        if not self.player.playing:
            self._fade, self._fade_start = 1.0, None  # music turned off: the next start is not faded
            self._apply_volume()
            return GLib.SOURCE_REMOVE
        self._fade = min((time.monotonic() - self._fade_start) / FADE_IN_SECONDS, 1.0)
        self._apply_volume()
        if self._fade < 1.0:
            return GLib.SOURCE_CONTINUE
        self._fade_start = None
        return GLib.SOURCE_REMOVE

    def _on_music_unavailable(self):
        if is_destroyed(self):
            return
        self.music_button.set_sensitive(False)
        self._show_music_state()
        self.music_button.set_icon_name("audio-volume-muted-symbolic")
        self.music_button.set_tooltip_text("No audio player found (pw-play, paplay or aplay)")
        if not self.pause_button.get_active():
            self.logo.go_on()
