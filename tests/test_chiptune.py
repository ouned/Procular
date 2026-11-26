import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from procular.util import chiptune as tune


def _rising_edges(samples):
    return sum(1 for a, b in zip(samples, samples[1:]) if b > a)


TRACKS = list(tune.TRACKS.values())
ids = list(tune.TRACKS)


@pytest.mark.parametrize("track", TRACKS, ids=ids)
def test_track_is_well_formed(track):
    assert len(track.bars) % 4 == 0
    assert len(track.bass) == tune.ROWS and len(track.arp) == tune.ROWS
    assert set(track.bass) <= set("ROFLT-.") and set(track.arp) <= set("x._")
    for bar in track.bars:
        for chord in bar.chords.split():
            tune.chord_notes(chord)
        assert tune.ROWS % len(bar.chords.split()) == 0
        tokens = bar.lead.split()
        assert len(tokens) == tune.ROWS and len(bar.drums) == tune.ROWS, bar
        assert set(bar.drums) <= set("KSShoc.")
        assert len(bar.bass or track.bass) == tune.ROWS
        for token in tokens:
            if token not in ("--", ".."):
                assert 48 <= tune.note_number(token) <= 96, token
    # the loop starts with a note: a hold in the first row would have nothing to hold
    assert track.bars[0].lead.split()[0] not in ("--", "..")


def test_note_numbers_and_frequencies():
    assert tune.note_number("A4") == 69
    assert tune.note_number("C#5") == 73
    assert tune.note_number("C3") == 48
    assert tune.note_number("Bb4") == 70
    assert tune.chord_notes("F#m") == (6, (0, 3, 7))
    assert tune.chord_notes("Bb") == (10, (0, 4, 7))
    assert tune.frequency(69) == pytest.approx(440.0)
    assert tune.frequency(81) == pytest.approx(880.0)


@pytest.mark.parametrize("hz, duty", [(110.0, 0.25), (440.0, 0.5), (1661.2, 0.125)])
def test_pulse_keeps_pitch_and_width_with_fractional_periods(hz, duty):
    # 44100 / 1661.2 is 26.5 samples: whole-sample periods would be a quarter tone off
    out = []
    tune.pulse(out, tune.RATE, tune.RATE / hz, duty, 0.0, b"\x00", b"\x01")
    samples = b"".join(out)
    assert len(samples) == tune.RATE
    assert _rising_edges(samples) == pytest.approx(hz, abs=1)
    assert sum(samples) / len(samples) == pytest.approx(duty, abs=0.002)


def test_pulse_continues_where_the_previous_tick_ended():
    whole, pieces = [], []
    tune.pulse(whole, 4000, 100.3, 0.25, 0.0, b"\x00", b"\x01")
    phase = 0.0
    for _ in range(5):
        phase = tune.pulse(pieces, 800, 100.3, 0.25, phase, b"\x00", b"\x01")
    assert b"".join(pieces) == b"".join(whole)


def test_mix_adds_sample_by_sample():
    assert tune.mix([bytes((10, 20, 30)), bytes((1, 2, 3)), bytes((100, 0, 222))]) == bytes((111, 22, 255))


@pytest.mark.parametrize("track", TRACKS, ids=ids)
def test_render_is_one_bar_per_chunk_within_its_headroom(track):
    bars = list(track.chunks())
    assert len(bars) == len(track.bars)
    assert all(len(bar) == track.bar_samples for bar in bars)
    samples = b"".join(bars)
    assert len(samples) / tune.RATE == pytest.approx(track.loop_seconds)
    # the voices are added without clipping, and the tune is not louder than intended
    assert tune.HEADROOM < 128
    assert 128 - tune.HEADROOM <= min(samples) and max(samples) <= 128 + tune.HEADROOM
    assert max(samples) - min(samples) > tune.HEADROOM  # and it is not silence
    assert samples == b"".join(track.chunks())  # no hidden state between two renderings


def test_bars_follow_their_chords():
    # the bass plays the chord's root (A2, F2, C3 in the first three bars) and its octave in turn
    track = tune.TRACKS["classic"]
    ticks = track._bass_ticks()
    eighth = 2 * track.speed
    for bar, hz in ((0, 110.0), (1, 87.31), (2, 130.81)):
        period, duty, volume = ticks[bar * track.bar_ticks]
        assert tune.RATE / period == pytest.approx(hz, rel=1e-3) and volume > 0 and duty == 0.25
        octave = ticks[bar * track.bar_ticks + eighth][0]
        assert period / octave == pytest.approx(2.0)
        # a short gap before the next note
        assert ticks[bar * track.bar_ticks + eighth - 1][2] == 0


def test_a_bar_can_hold_two_chords():
    track = tune.Track("test", [("F G", "F5 " + "-- " * 15, "." * 16)], arp="x" * 16, arp_rate=6, bass="R" * 16,
                       bass_gap=0)
    arp = track._arp_ticks()
    first = sorted({round(tune.RATE / period) for period, _d, _v in arp[:8 * 6]})
    second = sorted({round(tune.RATE / period) for period, _d, _v in arp[8 * 6:]})
    assert first == [round(tune.frequency(n)) for n in (69, 72, 77)]  # A4 C5 F5
    assert second == [round(tune.frequency(n)) for n in (67, 71, 74)]  # G4 B4 D5
    bass = track._bass_ticks()
    assert tune.RATE / bass[0][0] == pytest.approx(tune.frequency(41))  # F2
    assert tune.RATE / bass[-1][0] == pytest.approx(tune.frequency(43))  # G2


def test_silent_voices_add_up_to_the_middle_of_the_range():
    bars = [(chords, lead, "." * tune.ROWS) for chords, lead, *_ in tune.TRACKS["classic"].bars]
    track = tune.Track("silence", bars, lead_volume=(0,), bass_volume=(0,), arp_volume=(0,))
    assert set(b"".join(track.chunks())) == {128}


@pytest.mark.parametrize("track", TRACKS, ids=ids)
def test_cues_match_the_score(track):
    cues = track.cues()
    row = track.speed * tune.TICK / tune.RATE
    assert cues.loop == pytest.approx(len(track.bars) * tune.ROWS * row)
    assert cues.beat == pytest.approx(4 * row) and cues.bar == pytest.approx(16 * row)
    # one cue for every lead note that starts and every drum hit, in order
    tokens = [token for bar in track.bars for token in bar.lead.split()]
    assert [round(s / row) for s, _note in cues.notes] == [i for i, tok in enumerate(tokens) if tok not in ("--", "..")]
    assert [n for _s, n in cues.notes] == [tune.note_number(tok) for tok in tokens if tok not in ("--", "..")]
    drums = "".join(bar.drums for bar in track.bars)
    assert [(round(s / row), h) for s, h in cues.hits] == [(i, h) for i, h in enumerate(drums) if h != "."]
    assert cues.travel == pytest.approx((track.echo_rows or 2) * row)
    assert cues.low == min(n for _s, n in cues.notes) and cues.high == max(n for _s, n in cues.notes)


def test_a_note_is_heard_when_its_cue_says():
    track = tune.TRACKS["classic"]
    lead = track._lead_ticks()
    for seconds, note in track.cues().notes[:20]:
        tick = round(seconds * tune.RATE / tune.TICK)
        assert tune.RATE / lead[tick][0] == pytest.approx(tune.frequency(note)) and lead[tick][2] > 0
    echo = track._echo_ticks(lead)
    shift = round(track.cues().travel * tune.RATE / tune.TICK)
    assert echo[shift:] == [(p, 0.5, v) for p, _d, v in lead[:-shift]]


def test_write_wav(tmp_path):
    import wave

    track = tune.TRACKS["classic"]
    path = str(tmp_path / "theme.wav")
    track.write_wav(path)
    with wave.open(path, "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 1, tune.RATE)
        assert wav.getnframes() == len(track.bars) * track.bar_samples
