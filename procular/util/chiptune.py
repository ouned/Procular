"""The About dialog's theme: keygen style chiptunes, synthesised in code.

Nothing is sampled and no audio file ships with the application. Each tune is written down
below like a tracker module (one line per bar) and rendered the way an 8-bit sound chip
would play it: four pulse waves (lead, its echo, a chord arpeggio, bass) and a noise
generator for the drums, with volume, pulse width and vibrato stepped at the tick rate.

The logo animation dances to the tune: Track.cues() lists when every lead note, echo and
drum hit is heard, worked out from the same score the samples are rendered from.

Rendering avoids a Python loop over the samples, which would take seconds: a pulse wave
is a list of runs ("17 samples high, 34 samples low"), and the voices are mixed by adding
their sample buffers as big integers. One bar takes a few milliseconds.

Listen to them, or check a change:  python3 -m procular.util.chiptune [TRACK|all] [OUT.wav]
"""

import math
from collections import namedtuple

RATE = 44100
TICK = 800  # samples per tick: envelopes and arpeggios step 55 times per second
ROWS = 16  # rows per bar; a row is a sixteenth note

# Loudness of each voice in sample steps around its centre line. The voices are added
# without scaling, so the sum is the loudest the tune can get: well below the 127 an
# unsigned 8-bit sample allows, because square waves are much louder than they look.
LEAD, ECHO, ARP, BASS, DRUMS = 16, 5, 8, 13, 16
HEADROOM = LEAD + ECHO + ARP + BASS + DRUMS

_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
# chord quality -> semitones above the root
_QUALITIES = {"": (0, 4, 7), "m": (0, 3, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "sus2": (0, 2, 7),
              "sus4": (0, 5, 7)}


def note_number(name):
    """MIDI note number: "A4" -> 69, "C#5" -> 73, "Bb4" -> 70."""
    accidental = {"#": 1, "b": -1}.get(name[1], 0)
    return _SEMITONES[name[0]] + accidental + 12 * (int(name[2 if accidental else 1:]) + 1)


def chord_notes(chord):
    """Root pitch class (0..11) and the semitones above it: "F#m" -> (6, (0, 3, 7))."""
    accidental = {"#": 1, "b": -1}.get(chord[1:2], 0)
    root = (_SEMITONES[chord[0]] + accidental) % 12
    return root, _QUALITIES[chord[1 + abs(accidental):]]


def frequency(note):
    """Hz of a MIDI note number (fractions bend the pitch)."""
    return 440.0 * 2 ** ((note - 69) / 12)


def _level(volume, loudness):
    return round(volume * loudness / 15)


def _lowest_at_or_above(pitch_class, low):
    return low + (pitch_class - low) % 12


# A bar: chords (one, or several that share the bar equally: "F G"), the lead (one token per
# row: a note starts, "--" holds it, ".." is silence), the drums (one character per row: K kick,
# S snare, h hi-hat, o open hi-hat, c crash, . nothing) and, if it differs from the track's,
# the bass pattern.
Bar = namedtuple("Bar", "chords lead drums bass", defaults=(None,))

# When things happen in one round of a track, in seconds from its first sample; see Track.cues().
Cues = namedtuple("Cues", "loop beat bar notes hits travel low high")


class Track:
    """A tune and the way the sound chip plays it.

    speed: ticks per row; the tempo is 60 / (4 * speed * TICK / RATE) beats per minute.
    echo_rows: the echo repeats the lead this many rows later (0: no echo).
    bass: one character per row, relative to the chord: R root, O its octave, F fifth, L fifth
        below, T third; "-" holds the note, "." is silence. The last `bass_gap` ticks of a
        note are silent, so that repeated notes don't blur.
    arp: one character per row: x strikes the chord again, "." lets it ring, "_" mutes it.
        A new chord note every `arp_rate` ticks: 1 blurs three notes into one buzzing chord,
        a whole row's worth of ticks plays them as a broken chord.
    arp_low: the arpeggio's chord notes lie in the octave from this note up.
    """

    def __init__(self, title, bars, *, speed=6, echo_rows=3, lead_duty=0.25, lead_volume=(13, 15, 15, 14, 13, 12, 12, 11),
                 vibrato=0.22, vibrato_ticks=9, bass="R-O-R-O-R-O-R-O-", bass_duty=0.25, bass_gap=2,
                 bass_volume=(15, 15, 14, 13, 12, 11, 10, 9, 8, 7), arp="x.xxx.xxx.xxx.xx", arp_rate=1,
                 arp_volume=(10, 9, 8, 7, 6, 5, 4, 4, 3, 3, 2, 2, 1), arp_low="G4"):
        self.title = title
        self.bars = tuple(Bar(*bar) for bar in bars)
        self.speed = speed
        self.echo_rows = echo_rows
        self.lead_duty = lead_duty
        self.lead_volume = lead_volume
        self.vibrato = vibrato
        self.vibrato_ticks = vibrato_ticks
        self.bass = bass
        self.bass_duty = bass_duty
        self.bass_gap = bass_gap
        self.bass_volume = bass_volume
        self.arp = arp
        self.arp_rate = arp_rate
        self.arp_volume = arp_volume
        self.arp_low = note_number(arp_low)

    # ------------------------------------------------------------ timing
    @property
    def bar_ticks(self):
        return ROWS * self.speed

    @property
    def bar_samples(self):
        return self.bar_ticks * TICK

    @property
    def beat_seconds(self):
        return 4 * self.speed * TICK / RATE

    @property
    def bpm(self):
        return 60 / self.beat_seconds

    @property
    def loop_seconds(self):
        return len(self.bars) * self.bar_samples / RATE

    def _row_chords(self):
        """The chord on every row of the tune."""
        for bar in self.bars:
            names = bar.chords.split()
            for row in range(ROWS):
                yield names[row * len(names) // ROWS]

    # ------------------------------------------------------------ sequencing
    # Each voice becomes one (period in samples, pulse width, volume 0..15) triple per tick.
    _SILENT = (0.0, 0.5, 0)

    def _lead_notes(self):
        """(first tick, MIDI note) of every lead note, and None where the lead falls silent."""
        out = []
        for index, token in enumerate(token for bar in self.bars for token in bar.lead.split()):
            if token == "..":
                out.append((index * self.speed, None))
            elif token != "--":
                out.append((index * self.speed, note_number(token)))
        return out

    def _lead_ticks(self):
        ticks = []
        note, age = None, 0
        starts = dict(self._lead_notes())
        for tick in range(len(self.bars) * self.bar_ticks):
            if tick in starts:
                note, age = starts[tick], 0
            if note is None:
                ticks.append(self._SILENT)
                continue
            # a thin, bright first tick; then the pulse slowly widens and narrows
            duty = self.lead_duty / 2 if age == 0 else self.lead_duty
            if age > 8:
                duty += 0.06 * (1 - math.cos((age - 8) * math.tau / 48))
            bend = 0.0
            if age > 12:  # vibrato on held notes
                bend = self.vibrato * math.sin((age - 12) * math.tau / self.vibrato_ticks)
            volume = self.lead_volume[min(age, len(self.lead_volume) - 1)]
            ticks.append((RATE / frequency(note + bend), duty, volume))
            age += 1
        return ticks

    def _echo_ticks(self, lead):
        if not self.echo_rows:
            return [self._SILENT] * len(lead)
        shift = self.echo_rows * self.speed
        # the echo at the start of the tune is that of its end: it is played in a loop
        return [(period, 0.5, volume) for period, _duty, volume in lead[-shift:] + lead[:-shift]]

    def _arp_ticks(self):
        ticks = []
        age = None
        rows = zip(self._row_chords(), (step for bar in self.bars for step in self.arp))
        for row, (chord, step) in enumerate(rows):
            root, intervals = chord_notes(chord)
            notes = sorted(_lowest_at_or_above((root + i) % 12, self.arp_low) for i in intervals)
            periods = [RATE / frequency(note) for note in notes]
            if step == "x":
                age = 0
            elif step == "_":
                age = None
            for tick in range(self.speed):
                if age is None:
                    ticks.append(self._SILENT)
                    continue
                count = row * self.speed + tick
                volume = self.arp_volume[min(age, len(self.arp_volume) - 1)]
                ticks.append((periods[count // self.arp_rate % len(periods)], 0.5, volume))
                age += 1
        return ticks

    def _bass_ticks(self):
        intervals = {"R": 0, "O": 12, "F": 7, "L": -5}
        notes = []  # [first tick, MIDI note or None]
        for row, (chord, (bar, step)) in enumerate(zip(
                self._row_chords(), ((bar, step) for bar in self.bars for step in (bar.bass or self.bass)))):
            if step == "-":
                continue
            if step == ".":
                notes.append((row * self.speed, None))
                continue
            root, chord_intervals = chord_notes(chord)
            bass = _lowest_at_or_above(root, note_number("E2"))
            notes.append((row * self.speed, bass + (chord_intervals[1] if step == "T" else intervals[step])))
        total = len(self.bars) * self.bar_ticks
        ticks = [self._SILENT] * total
        for (start, note), (end, _next) in zip(notes, notes[1:] + [(total, None)]):
            if note is None:
                continue
            period = RATE / frequency(note)
            sounding = end - start - self.bass_gap if end - start > self.bass_gap else end - start
            for age in range(sounding):
                ticks[start + age] = (period, self.bass_duty, self.bass_volume[min(age, len(self.bass_volume) - 1)])
        return ticks

    # ------------------------------------------------------------ the animation's cues
    def cues(self):
        """What the logo animation needs to dance to one round of the tune (see logo_animation).

        notes: (seconds, MIDI note) when every lead note starts; hits: (seconds, drum) of every
        drum hit; travel: seconds from a note to its echo; low, high: the lead's range.
        """
        tick_seconds = TICK / RATE
        notes = tuple((tick * tick_seconds, note) for tick, note in self._lead_notes() if note is not None)
        row_seconds = self.speed * tick_seconds
        hits = tuple(((bar * ROWS + row) * row_seconds, hit) for bar, line in enumerate(self.bars)
                     for row, hit in enumerate(line.drums) if hit != ".")
        pitches = [note for _t, note in notes]
        return Cues(loop=self.loop_seconds, beat=self.beat_seconds, bar=ROWS * row_seconds, notes=notes, hits=hits,
                    travel=(self.echo_rows or 2) * row_seconds, low=min(pitches), high=max(pitches))

    # ------------------------------------------------------------ rendering
    def chunks(self):
        """One pass through the tune, a bar at a time: unsigned 8-bit mono samples at RATE.

        Made to be played in a loop; the echo at the start is that of the last bar.
        """
        lead = self._lead_ticks()
        # every voice swings around its own centre line; together the lines add up to the
        # middle of the sample range, 128 (the drums' line takes up the rest)
        voices = []
        for ticks, loudness in ((lead, LEAD), (self._echo_ticks(lead), ECHO), (self._arp_ticks(), ARP),
                                (self._bass_ticks(), BASS)):
            voices.append({"ticks": ticks, "loudness": loudness, "phase": 0.0, "rest": bytes((loudness,)) * TICK})
        drum_centre = 128 - (HEADROOM - DRUMS)
        hits = _drum_hits(drum_centre)

        bar_ticks, bar_samples = self.bar_ticks, self.bar_samples
        for index, bar in enumerate(self.bars):
            channels = []
            for voice in voices:
                out = []
                centre = voice["loudness"]
                for period, duty, volume in voice["ticks"][index * bar_ticks:(index + 1) * bar_ticks]:
                    level = _level(volume, centre)
                    if level == 0:
                        out.append(voice["rest"])
                    else:
                        voice["phase"] = pulse(out, TICK, period, duty, voice["phase"],
                                               bytes((centre - level,)), bytes((centre + level,)))
                channels.append(b"".join(out))
            track = bytearray(bytes((drum_centre,)) * bar_samples)
            for row, hit in enumerate(bar.drums):
                if hit in hits:
                    start = row * self.speed * TICK
                    sound = hits[hit][:bar_samples - start]
                    track[start:start + len(sound)] = sound
            channels.append(bytes(track))
            yield mix(channels)

    def write_wav(self, path, loops=1):
        import wave

        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(RATE)
            wav.writeframes(b"".join(self.chunks()) * loops)


# ---------------------------------------------------------------- synthesis
def pulse(out, count, period, duty, phase, low, high):
    """Append `count` samples of a pulse wave to the list `out`, as runs of the bytes `high` and `low`.

    period: samples per cycle, need not be whole: the runs alternate between the two
    nearest lengths, which keeps the pitch exact. phase: where in the cycle to start
    (0..1); returns where the next call has to continue.
    """
    position = phase * period
    split = duty * period
    while count > 0:
        if position < split:
            run, piece = math.ceil(split - position), high
        else:
            run, piece = math.ceil(period - position), low
        run = min(max(run, 1), count)
        out.append(piece * run)
        position += run
        if position >= period:
            position -= period
        count -= run
    return position / period


def _noise(out, count, period, low, high, state):
    """Append `count` samples of shift register noise (the NES kind); returns the register."""
    lfsr, fraction = state
    while count > 0:
        fraction += period
        run = min(max(int(fraction), 1), count)
        fraction -= int(fraction)
        out.append((high if lfsr & 1 else low) * run)
        lfsr = (lfsr >> 1) | (((lfsr ^ (lfsr >> 1)) & 1) << 14)
        count -= run
    return lfsr, fraction


def _drum_hits(centre):
    """The drum sounds, rendered once: {"K": bytes, "S": bytes, ...}."""

    def render(kind, steps):
        out = []
        phase, state = 0.0, (0x4A31, 0.0)
        for value, volume in steps:
            level = _level(volume, DRUMS)
            low, high = bytes((centre - level,)), bytes((centre + level,))
            if kind == "tone":
                phase = pulse(out, TICK, RATE / value, 0.5, phase, low, high)
            else:
                state = _noise(out, TICK, value, low, high, state)
        return b"".join(out)

    return {
        # a square wave falling from 220 Hz to a thud
        "K": render("tone", ((220, 15), (150, 15), (105, 14), (75, 12), (58, 9), (48, 6))),
        # noise: coarse for the body, then finer as it fades
        "S": render("noise", ((9, 15), (5, 13), (5, 10), (6, 7), (6, 4))),
        "h": render("noise", ((2, 7), (2, 3))),
        "o": render("noise", ((2, 7), (2, 6), (2, 5), (2, 4), (2, 3), (2, 2), (2, 1))),
        "c": render("noise", ((3, 12), (3, 11), (2, 10), (2, 9), (2, 8), (2, 7), (2, 6), (2, 5), (2, 5), (2, 4),
                              (2, 4), (2, 3), (2, 3), (2, 2), (2, 2), (2, 1))),
    }


def mix(channels):
    """Add equally long sample buffers. The sums must fit a byte: lanes of one big integer don't carry."""
    total = 0
    for channel in channels:
        total += int.from_bytes(channel, "little")
    return total.to_bytes(len(channels[0]), "little")


# ---------------------------------------------------------------- the tunes
_BEAT = "K.h.S.h.K.hKS.h."
_FILL = "K.h.S.h.K.S.SSSS"

CLASSIC = Track("Classic", (
    ("Am", "E5 -- -- E5 -- -- D5 -- E5 -- -- A5 -- -- -- --", _BEAT),
    ("F", "A5 -- G5 -- F5 -- E5 -- F5 -- -- C5 -- -- -- --", _BEAT),
    ("C", "G5 -- -- G5 -- -- F5 -- G5 -- -- C6 -- -- -- --", _BEAT),
    ("G", "B5 -- A5 -- G5 -- A5 -- B5 -- -- D6 -- -- B5 --", _BEAT),
    ("Am", "C6 -- -- C6 -- -- B5 -- C6 -- -- E6 -- -- -- --", _BEAT),
    ("F", "D6 -- C6 -- A5 -- G5 -- A5 -- -- F5 -- -- -- --", _BEAT),
    ("G", "G5 -- -- B5 -- -- D6 -- B5 -- -- G5 -- -- B5 --", _BEAT),
    ("E", "G#5 -- -- B5 -- -- E6 -- D6 -- B5 -- G#5 -- B5 --", _FILL),
    ("F", "C6 -- -- -- -- -- A5 -- F5 -- A5 -- C6 -- -- --", _BEAT),
    ("G", "D6 -- -- -- -- -- B5 -- G5 -- B5 -- D6 -- -- --", _BEAT),
    ("Em", "E6 -- -- -- -- -- D6 -- B5 -- -- G5 -- -- B5 --", _BEAT),
    ("Am", "C6 -- -- B5 -- -- A5 -- -- -- -- -- E5 -- A5 --", _BEAT),
    ("F", "C6 -- -- -- -- -- A5 -- F5 -- A5 -- C6 -- F6 --", _BEAT),
    ("G", "D6 -- -- -- -- -- B5 -- G5 -- B5 -- D6 -- G6 --", _BEAT),
    ("Am", "E6 -- -- E6 -- -- D6 -- C6 -- -- B5 -- -- A5 --", _BEAT),
    ("E", "G#5 -- B5 -- E6 -- B5 -- E5 G#5 B5 E6 G#5 B5 E6 G#6", _FILL),
))

# 165 BPM trance: four on the floor, a bass on the off-beats, a gated chord
_FLOOR = "K.o.K.o.K.o.K.o."
_FLOOR_FILL = "K.o.K.o.SSS.SSSS"
_BREAK = "K.h.K.h.K.h.K.hh"
OVERCLOCK = Track("Overclock", (
    ("Em", "E5 -- -- G5 -- -- B5 -- E6 -- -- D6 -- -- B5 --", "c.o.K.o.K.o.K.o."),
    ("C", "C6 -- -- B5 -- -- G5 -- E5 -- -- G5 -- -- C6 --", _FLOOR),
    ("G", "D6 -- -- B5 -- -- G5 -- D6 -- -- E6 -- -- D6 --", _FLOOR),
    ("D", "F#6 -- -- -- -- -- E6 -- D6 -- -- A5 -- -- F#5 --", _FLOOR),
    ("Em", "E5 -- -- G5 -- -- B5 -- E6 -- -- F#6 -- -- G6 --", _FLOOR),
    ("C", "G6 -- -- E6 -- -- C6 -- G5 -- -- C6 -- -- E6 --", _FLOOR),
    ("Am", "E6 -- -- C6 -- -- A5 -- C6 -- -- E6 -- -- A6 --", _FLOOR),
    ("B", "F#6 -- -- -- D#6 -- -- -- B5 -- -- -- F#5 -- A5 --", _FLOOR_FILL),
    ("C", "E6 -- -- -- -- -- D6 -- C6 -- -- -- B5 -- C6 --", _BREAK),
    ("D", "D6 -- -- -- -- -- C6 -- B5 -- -- -- A5 -- B5 --", _BREAK),
    ("Em", "B5 -- -- -- -- -- -- -- E5 -- G5 -- B5 -- E6 --", _BREAK),
    ("Em", "D6 -- -- -- -- -- B5 -- -- -- G5 -- -- -- -- --", "K.h.K.h.K.S.SSSS"),
    ("C", "E6 -- -- -- -- -- D6 -- C6 -- -- -- B5 -- C6 --", "c.o.K.o.K.o.K.o."),
    ("D", "D6 -- -- -- -- -- E6 -- F#6 -- -- -- A6 -- F#6 --", _FLOOR),
    ("B", "D#6 -- -- -- -- -- F#6 -- B6 -- -- -- A6 -- F#6 --", _FLOOR),
    ("B", "D#6 -- B5 -- F#5 -- D#5 -- B4 D#5 F#5 B5 D#6 F#6 A6 B6", _FLOOR_FILL),
), speed=5, bass="..R-..O-..R-..O-", bass_gap=1, arp="xxxxxxxxxxxxxxxx", arp_volume=(10, 7, 4, 2, 0),
    arp_low="B4")

# 118 BPM, moody: a half-time beat, a broad hollow lead with a slow vibrato, a slow buzzing pad
_HALF = "K.h.h.h.S.h.hKh."
_HALF_FILL = "K.h.h.h.S.h.SSSS"
NIGHT_SHIFT = Track("Night Shift", (
    ("Dm", "D6 -- -- -- -- -- A5 -- -- -- F5 -- A5 -- D6 --", "c.h.h.h.S.h.hKh."),
    ("Bb", "F6 -- -- -- -- -- D6 -- -- -- Bb5 -- -- -- C6 --", _HALF),
    ("F", "C6 -- -- -- -- -- A5 -- -- -- F5 -- -- -- G5 --", _HALF),
    ("C", "E5 -- -- -- -- -- -- -- G5 -- -- -- C6 -- E6 --", _HALF),
    ("Dm", "F6 -- -- -- -- -- E6 -- D6 -- -- -- A5 -- -- --", _HALF),
    ("Bb", "D6 -- -- -- -- -- C6 -- Bb5 -- -- -- F5 -- -- --", _HALF),
    ("Gm", "G5 -- -- -- Bb5 -- -- -- D6 -- -- -- G6 -- F6 --", _HALF),
    ("A", "E6 -- -- -- -- -- C#6 -- -- -- A5 -- -- -- -- --", _HALF_FILL),
    ("Bb", "D6 -- -- F6 -- -- D6 -- Bb5 -- -- D6 -- -- F6 --", _HALF),
    ("C", "E6 -- -- G6 -- -- E6 -- C6 -- -- E6 -- -- G6 --", _HALF),
    ("Dm", "A6 -- -- -- -- -- -- -- -- -- -- -- F6 -- E6 --", _HALF),
    ("Dm", "D6 -- -- -- -- -- -- -- .. .. A5 -- D6 -- F6 --", _HALF),
    ("Gm", "G6 -- -- -- -- -- F6 -- D6 -- -- -- Bb5 -- -- --", _HALF),
    ("A", "A5 -- -- -- C#6 -- -- -- E6 -- -- -- G6 -- -- --", _HALF),
    ("Dm", "F6 -- -- -- E6 -- -- -- D6 -- -- -- A5 -- -- --", _HALF),
    ("A", "C#6 -- -- -- -- -- -- -- E5 A5 C#6 E6 A6 E6 C#6 A5", _HALF_FILL),
), speed=7, lead_duty=0.5, vibrato=0.3, vibrato_ticks=14, bass="R--R--R-R--R--O-",
    arp="x.......x.......", arp_rate=2,
    arp_volume=(6, 8, 9, 9, 8, 8, 7, 7, 7, 6, 6, 6, 5, 5, 5, 5, 4, 4, 4, 4, 3, 3, 3, 3, 3, 2), arp_low="A4")

# 138 BPM, sunny: a bouncing bass, a thin bright lead echoed an eighth later, off-beat chords
_POP = "K.h.S.hKK.h.S.hh"
_POP_FILL = "K.h.S.h.S.SSK.SS"
UPTIME = Track("Uptime", (
    ("C", "E5 -- G5 -- C6 -- -- G5 -- -- E6 -- D6 -- C6 --", "c.h.S.hKK.h.S.hh"),
    ("G", "D6 -- -- B5 -- -- G5 -- D6 -- -- -- B5 -- -- --", _POP),
    ("Am", "C6 -- E6 -- A6 -- -- E6 -- -- C6 -- B5 -- A5 --", _POP),
    ("F", "A5 -- -- -- C6 -- -- -- F6 -- E6 -- D6 -- C6 --", _POP),
    ("C", "G6 -- -- E6 -- -- C6 -- G6 -- -- E6 -- -- C6 --", _POP),
    ("G", "B5 -- -- D6 -- -- G6 -- F6 -- -- D6 -- -- B5 --", _POP),
    ("Am", "C6 -- -- -- E6 -- -- -- A6 -- G6 -- E6 -- C6 --", _POP),
    ("F G", "F6 -- -- -- E6 -- D6 -- D6 -- -- -- B5 -- D6 --", _POP_FILL),
    ("F", "A5 -- -- -- -- -- C6 -- -- -- F6 -- -- -- E6 --", _POP),
    ("G", "D6 -- -- -- -- -- B5 -- -- -- G5 -- -- -- B5 --", _POP),
    ("Em Am", "E6 -- -- -- B5 -- -- -- C6 -- -- -- E6 -- -- --", _POP),
    ("Dm G", "F6 -- -- -- D6 -- -- -- B5 -- -- -- D6 -- G6 --", _POP),
    ("F", "A6 -- -- -- -- -- G6 -- F6 -- -- -- C6 -- -- --", _POP),
    ("G", "B5 -- -- -- -- -- D6 -- G6 -- -- -- F6 -- -- --", _POP),
    ("C Am", "E6 -- -- -- G6 -- -- -- E6 -- -- -- C6 -- -- --", _POP),
    ("F G", "C6 -- A5 -- F5 -- A5 -- B5 -- D6 -- F6 -- G6 --", _POP_FILL),
), echo_rows=2, lead_duty=0.125, bass="R-F-O-F-R-F-O-F-", arp="..x...x...x...x.",
    arp_volume=(9, 8, 6, 4, 3, 2, 1, 0))

# 103 BPM, eerie: harmonic minor, a slow wobbling lead, the chord picked one note per row
_CREEP = "K..h..hKS..h..h."
ZOMBIE_PROCESS = Track("Zombie Process", (
    ("Am", "A5 -- -- -- C6 -- -- B5 -- -- A5 -- E5 -- -- --", "c..h..hKS..h..h."),
    ("Dm", "F5 -- -- -- A5 -- -- G#5 -- -- F5 -- D5 -- -- --", _CREEP),
    ("E", "E5 -- G#5 -- B5 -- D6 -- -- -- C6 -- B5 -- -- --", _CREEP),
    ("Am", "A5 -- -- -- -- -- -- -- .. .. E5 -- A5 -- C6 --", _CREEP),
    ("F", "F6 -- -- -- E6 -- -- -- D6 -- -- C6 -- -- A5 --", _CREEP),
    ("Dm", "D6 -- -- -- F6 -- -- -- A6 -- -- G#6 -- -- F6 --", _CREEP),
    ("E", "E6 -- -- -- -- -- D6 -- C6 -- -- -- B5 -- -- --", _CREEP),
    ("E", "G#5 -- -- -- B5 -- -- -- E6 -- -- -- -- -- -- --", "K..h..hKS.SS.SSS"),
    ("Am", "A6 -- -- E6 -- -- C6 -- A5 -- -- C6 -- -- E6 --", _CREEP),
    ("G", "D6 -- -- B5 -- -- G5 -- D6 -- -- B5 -- -- G5 --", _CREEP),
    ("F", "C6 -- -- A5 -- -- F5 -- C6 -- -- A5 -- -- F6 --", _CREEP),
    ("E", "E6 -- -- -- -- -- D6 -- -- -- C6 -- -- -- B5 --", _CREEP),
    ("Dm", "A5 -- -- -- -- -- D6 -- -- -- F6 -- -- -- A6 --", _CREEP),
    ("F", "A6 -- -- -- -- -- G6 -- F6 -- -- -- C6 -- -- --", _CREEP),
    ("E", "B5 -- -- -- -- -- G#5 -- -- -- E5 -- -- -- D6 --", _CREEP),
    ("E", "C6 -- B5 -- -- -- G#5 -- E5 F5 G#5 B5 D6 E6 F6 G#6", "K..h..hKS.SS.SSS"),
), speed=8, echo_rows=4, lead_duty=0.5, vibrato=0.35, vibrato_ticks=20, bass="R--R--O-R--R--L-",
    arp="xxxxxxxxxxxxxxxx", arp_rate=8, arp_volume=(9, 8, 7, 6, 5, 4, 4, 3), arp_low="E4")

# 206 BPM breakbeat: a two-step drum loop, a long sustained bass, a rolling riff in the middle
_STEP = "K.h.S.h.h.KhS.hh"
_STEP_FILL = "K.h.S.h.SSKSSSSS"
HYPERTHREAD = Track("Hyperthread", (
    ("Bm", "B5 -- -- -- -- -- -- -- F#5 -- -- -- B5 -- D6 --", "c.h.S.h.h.KhS.hh"),
    ("G", "D6 -- -- -- -- -- B5 -- -- -- G5 -- -- -- B5 --", _STEP),
    ("D", "A5 -- -- -- -- -- F#5 -- -- -- D5 -- -- -- F#5 --", _STEP),
    ("A", "E5 -- -- -- -- -- -- -- C#6 -- -- -- E6 -- -- --", _STEP),
    ("Bm", "F#6 -- -- -- -- -- E6 -- D6 -- -- -- B5 -- -- --", _STEP),
    ("G", "D6 -- -- -- -- -- E6 -- -- -- F#6 -- -- -- G6 --", _STEP),
    ("Em", "G6 -- -- -- -- -- F#6 -- E6 -- -- -- B5 -- -- --", _STEP),
    ("F#", "A#5 -- -- -- C#6 -- -- -- F#6 -- -- -- E6 -- C#6 --", _STEP_FILL),
    ("Bm", "B5 D6 F#6 D6 B5 D6 F#6 D6 B5 D6 F#6 D6 B6 -- F#6 --", "c.h.S.h.h.KhS.hh"),
    ("G", "B5 D6 G6 D6 B5 D6 G6 D6 B5 D6 G6 D6 B6 -- G6 --", _STEP),
    ("D", "A5 D6 F#6 D6 A5 D6 F#6 D6 A5 D6 F#6 D6 A6 -- F#6 --", _STEP),
    ("A", "A5 C#6 E6 C#6 A5 C#6 E6 C#6 A5 C#6 E6 C#6 A6 -- E6 --", _STEP_FILL),
    ("Bm", "F#6 -- -- -- -- -- -- -- D6 -- -- -- B5 -- -- --", _STEP),
    ("G", "G6 -- -- -- -- -- -- -- E6 -- -- -- B5 -- -- --", _STEP),
    ("Em", "E6 -- -- -- G6 -- -- -- B6 -- -- -- G6 -- -- --", _STEP),
    ("F#", "A#6 -- -- -- -- -- -- -- F#6 -- C#6 -- A#5 -- F#5 --", _STEP_FILL),
), speed=4, bass="R---------R---O-", bass_duty=0.5, bass_volume=(15, 15, 14, 13, 12, 12, 11),
    arp="x...x...x...x...", arp_volume=(9, 8, 7, 6, 5, 4, 3, 3, 2, 2, 1, 1, 0), arp_low="A4")

TRACKS = {"classic": CLASSIC, "overclock": OVERCLOCK, "night-shift": NIGHT_SHIFT, "uptime": UPTIME,
          "zombie-process": ZOMBIE_PROCESS, "hyperthread": HYPERTHREAD}
DEFAULT_TRACK = "hyperthread"


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK
    for key in TRACKS if name == "all" else (name,):
        path = sys.argv[2] if len(sys.argv) > 2 and name != "all" else f"procular-{key}.wav"
        TRACKS[key].write_wav(path, loops=2)
        print(f"{path}: {TRACKS[key].title}, {TRACKS[key].bpm:.0f} BPM, {TRACKS[key].loop_seconds:.1f} s")
