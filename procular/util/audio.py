"""Endless playback of generated audio through the desktop's command line players.

GTK's own media support needs a GStreamer backend package that is often missing, and
GStreamer's Python bindings are no dependency of ours. pw-play, paplay and aplay are there
on every desktop with sound; they take raw samples on standard input, so nothing is written
to disk and a loop has no gap: the samples of the next round simply follow.

The volume is applied here, on the way to the player, because aplay has no volume option and
none of the players can change it while playing. The samples go out in small pieces through
a pipe that holds just one of them, so a new volume is heard within a fraction of a second.
"""

import fcntl
import shutil
import subprocess
import threading
import time

from gi.repository import GLib

# Tried in this order; "{rate}" is filled in. All read signed 16-bit mono from standard input.
PLAYERS = (
    # PipeWire's own player, twice: current versions want --raw for samples without a file header,
    # older ones (1.0, Ubuntu 24.04) don't know the option and take standard input as raw anyway.
    # The one that doesn't fit exits at once and the next player gets its turn.
    ("pw-play", "--raw", "--format", "s16", "--rate", "{rate}", "--channels", "1", "-"),
    ("pw-play", "--format", "s16", "--rate", "{rate}", "--channels", "1", "-"),
    ("paplay", "--raw", "--format=s16le", "--rate={rate}", "--channels=1", "--latency-msec=100"),
    ("aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "{rate}", "-c", "1", "-"),
)
# samples written at a time, and held by the pipe: 46 ms at 44.1 kHz. As 16-bit samples
# they are 4096 bytes, the smallest size a pipe can have.
PIECE = 2048
# a player that gives up sooner than this never played: wrong options or no sound server
GAVE_UP_SECONDS = 2.0

_running = set()


def stop_all():
    """Silence every player (application shutdown)."""
    for player in list(_running):
        player.stop()


def gain_tables(gain):
    """Lookup tables for to_s16(): the low and the high byte of every 8-bit sample times `gain` (0..1)."""
    values = [max(-32768, min(32767, round((sample - 128) * 256 * gain))) for sample in range(256)]
    return bytes(v & 0xFF for v in values), bytes((v >> 8) & 0xFF for v in values)


def to_s16(samples, tables):
    """Unsigned 8-bit samples (bytes) as signed 16-bit little endian ones, scaled by the tables' gain.

    16 bits, because 8-bit samples turned down have too few steps left: quiet voices would
    drop out and the rest would sound rough.
    """
    out = bytearray(2 * len(samples))
    out[0::2] = samples.translate(tables[0])
    out[1::2] = samples.translate(tables[1])
    return out


class _Session:
    """One start() .. stop() of a LoopPlayer: lets the worker thread see that it was stopped."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = False
        self.process = None

    def attach(self, process):
        with self.lock:
            if not self.stopped:
                self.process = process
            return not self.stopped

    def stop(self):
        with self.lock:
            self.stopped = True
            process = self.process
        if process is not None:
            process.kill()  # the worker's write fails with a broken pipe; it reaps the process


class LoopPlayer:
    """Plays what `source` renders over and over until stop().

    source: callable returning the chunks (bytes, unsigned 8-bit mono at `rate`) of one
        round. It runs in a worker thread and is asked once per start(); the rounds after
        the first replay its chunks. The player is fed in step with the music, so a
        generator renders just ahead of it.
    volume: gain 0..1 (1: the samples as rendered); may be changed while playing.
    on_started(monotonic seconds): a player took the first samples.
    on_unavailable(): no player could be started. Both are called on the main loop.
    """

    def __init__(self, source, rate, volume=1.0, on_started=None, on_unavailable=None, players=PLAYERS):
        self._source = source
        self._rate = rate
        self.volume = volume
        self._on_started = on_started
        self._on_unavailable = on_unavailable
        self._players = players
        self._session = None

    @property
    def playing(self):
        return self._session is not None

    def start(self, offset=0):
        """Play from sample number `offset` of the round on (the rounds after it are whole)."""
        if self._session is not None:
            return
        self._session = session = _Session()
        _running.add(self)
        threading.Thread(target=self._run, args=(session, offset), name="procular-audio", daemon=True).start()

    def stop(self):
        session, self._session = self._session, None
        _running.discard(self)
        if session is not None:
            session.stop()

    # ------------------------------------------------------------------ worker thread
    def _run(self, session, offset):
        rendered = []
        pending = iter(self._source())

        def rounds():
            yield from tuple(rendered)  # a second player starts the tune from the top (or the offset)
            for chunk in pending:
                rendered.append(chunk)
                yield chunk
            while rendered:
                yield from rendered

        def pieces():
            gain = tables = None
            skip = offset
            for chunk in rounds():
                if skip:
                    chunk, skip = chunk[skip:], max(0, skip - len(chunk))
                for start in range(0, len(chunk), PIECE):
                    if gain != self.volume:
                        gain = self.volume
                        tables = gain_tables(min(max(gain, 0.0), 1.0))
                    yield to_s16(chunk[start:start + PIECE], tables)

        for command in self._players:
            if session.stopped:
                return
            if shutil.which(command[0]) is None:
                continue
            argv = [arg.format(rate=self._rate) for arg in command]
            try:
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            except OSError:
                continue
            started = time.monotonic()
            try:
                if not session.attach(process):
                    return
                try:
                    fcntl.fcntl(process.stdin, fcntl.F_SETPIPE_SZ, 2 * PIECE)
                    capacity = fcntl.fcntl(process.stdin, fcntl.F_GETPIPE_SZ)
                except (AttributeError, OSError):
                    capacity = 65536  # the default holds 0.7 s: volume changes are heard that much later
                # one piece more than the pipe holds can only be written once the player has read
                # some: until then it may be a player that exits without playing
                proof = capacity // (2 * PIECE)
                for count, piece in enumerate(pieces()):
                    if session.stopped:
                        break
                    process.stdin.write(piece)
                    process.stdin.flush()
                    if count == proof:
                        self._notify(session, self._on_started, time.monotonic())
            except OSError:
                pass  # broken pipe: stopped, or the player exited
            finally:
                process.kill()
                try:
                    process.stdin.close()
                except OSError:
                    pass
                process.wait()  # no zombie in our own process list
            if session.stopped or time.monotonic() - started > GAVE_UP_SECONDS:
                return  # it did play and then lost its output: don't start over on another player
        self._notify(session, self._unavailable)

    def _unavailable(self):
        self.stop()
        if self._on_unavailable is not None:
            self._on_unavailable()

    @staticmethod
    def _notify(session, callback, *args):
        def deliver():
            if not session.stopped:  # else: stopped (or started again) in the meantime
                callback(*args)
            return GLib.SOURCE_REMOVE

        if callback is not None:
            GLib.idle_add(deliver)
