import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

pytest.importorskip("gi")

from gi.repository import GLib  # noqa: E402

from procular.util import audio  # noqa: E402

CHUNK = bytes(range(256)) * 270  # 8-bit samples; the players get twice as many bytes (16-bit)
FULL = audio.gain_tables(1.0)


def _spin(until, timeout=5.0):
    """Run the main loop (the player's callbacks arrive there) until until() is true."""
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


def _children():
    out = subprocess.run(["ps", "--ppid", str(os.getpid()), "-o", "stat=,comm="], capture_output=True, text=True)
    return [line.split() for line in out.stdout.splitlines() if line.split()[1] != "ps"]


def _source(calls, rounds=3):
    def source():
        calls.append("render")
        for _ in range(rounds):
            yield CHUNK

    return source


def test_loops_what_the_source_rendered_once(tmp_path):
    out = tmp_path / "played"
    calls, started = [], []
    player = audio.LoopPlayer(_source(calls), 44100, on_started=started.append,
                              players=(("sh", "-c", f"exec cat > {out}"),))
    player.start()
    try:
        assert _spin(lambda: out.exists() and out.stat().st_size > 16 * len(CHUNK))  # well past the 3 rendered chunks
        assert calls == ["render"]
        assert _spin(lambda: bool(started)) and player.playing
    finally:
        player.stop()
    assert not player.playing
    assert _spin(lambda: not _children()), _children()  # killed and reaped: no zombie


def test_falls_back_to_the_next_player(tmp_path):
    out = tmp_path / "played"
    events = []
    players = (("procular-no-such-player",), ("false",), ("sh", "-c", f"exec cat > {out}"))
    player = audio.LoopPlayer(_source([]), 44100, on_started=lambda _t: events.append("started"),
                              on_unavailable=lambda: events.append("unavailable"), players=players)
    player.start()
    try:
        assert _spin(lambda: out.exists() and out.stat().st_size >= 4 * len(CHUNK))
        assert _spin(lambda: bool(events)) and events == ["started"]
    finally:
        player.stop()
    # the player that worked got the tune from its first sample
    assert out.read_bytes()[:2 * len(CHUNK)] == audio.to_s16(CHUNK, FULL)
    assert _spin(lambda: not _children())


def test_reports_when_nothing_can_play():
    events = []
    player = audio.LoopPlayer(_source([]), 44100, on_started=lambda _t: events.append("started"),
                              on_unavailable=lambda: events.append("unavailable"),
                              players=(("procular-no-such-player",), ("false",)))
    player.start()
    assert _spin(lambda: bool(events))
    assert events == ["unavailable"] and not player.playing
    assert _spin(lambda: not _children())


def test_stop_all_silences_running_players():
    player = audio.LoopPlayer(_source([]), 44100, players=(("sh", "-c", "exec cat > /dev/null"),))
    player.start()
    assert _spin(lambda: bool(_children()))
    audio.stop_all()
    assert not player.playing
    assert _spin(lambda: not _children())


def _s16(data):
    return [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data), 2)]


def test_gain_scales_8_bit_samples_to_16_bit():
    samples = bytes((0, 64, 128, 129, 255))
    assert _s16(audio.to_s16(samples, audio.gain_tables(1.0))) == [-32768, -16384, 0, 256, 32512]
    assert _s16(audio.to_s16(samples, audio.gain_tables(0.25))) == [-8192, -4096, 0, 64, 8128]
    # turned far down, neighbouring 8-bit steps are still told apart
    assert _s16(audio.to_s16(samples, audio.gain_tables(0.01))) == [-328, -164, 0, 3, 325]
    assert _s16(audio.to_s16(samples, audio.gain_tables(0.0))) == [0] * 5


def test_volume_changes_while_playing(tmp_path):
    out = tmp_path / "played"
    player = audio.LoopPlayer(lambda: [b"\xff" * 70000], 44100, volume=1.0,
                              players=(("sh", "-c", f"exec cat > {out}"),))
    player.start()
    try:
        assert _spin(lambda: out.exists() and out.stat().st_size > 4 * audio.PIECE)
        assert _s16(out.read_bytes()[:4]) == [32512, 32512]
        player.volume = 0.5
        size = out.stat().st_size
        assert _spin(lambda: out.stat().st_size > size + 8 * audio.PIECE)
    finally:
        player.stop()
    played = out.read_bytes()
    assert _s16(played[len(played) // 2 * 2 - 4:len(played) // 2 * 2]) == [16256, 16256]
    assert _spin(lambda: not _children())


def test_starts_at_an_offset_and_loops_from_the_top(tmp_path):
    out = tmp_path / "played"
    chunks = [bytes((i,)) * 3000 for i in (10, 20, 30)]
    player = audio.LoopPlayer(lambda: iter(chunks), 44100, players=(("sh", "-c", f"exec cat > {out}"),))
    player.start(4000)  # a third into the second chunk
    try:
        assert _spin(lambda: out.exists() and out.stat().st_size > 2 * 12000)
    finally:
        player.stop()
    expected = audio.to_s16(chunks[1][1000:] + chunks[2] + b"".join(chunks), FULL)
    assert out.read_bytes()[:len(expected)] == expected
    assert _spin(lambda: not _children())
