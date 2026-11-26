import pytest

pytest.importorskip("gi")

from procular.config import Config  # noqa: E402
from procular.providers import sampler as sampler_mod  # noqa: E402


@pytest.fixture
def sampler(tmp_path):
    return sampler_mod.Sampler(Config(path=str(tmp_path / "settings.json")))


def test_background_slows_the_interval_down(sampler):
    cfg = sampler.config
    assert sampler._interval_ms() == cfg.get("interval_ms") == 1000
    sampler.set_background(True)
    assert sampler._interval_ms() == sampler_mod.BACKGROUND_INTERVAL_MS
    assert sampler.interval == sampler_mod.BACKGROUND_INTERVAL_MS / 1000
    cfg.set("interval_ms", 10000)  # already slower than the background rate: left alone
    assert sampler._interval_ms() == 10000
    cfg.set("interval_ms", 500)
    cfg.set("background_slowdown", False)
    assert sampler._interval_ms() == 500
    cfg.set("background_slowdown", True)
    sampler.set_background(False)
    assert sampler._interval_ms() == 500


def test_details_are_only_sampled_on_request(sampler):
    system = sampler.system
    snap = system.sample()
    assert snap["net_info"] is None and snap["disk_info"] is None
    assert snap["freq"] is None and snap["governor"] is None
    assert "wifi_quality" in snap and snap["cpu_cores"]
    system.set_detailed(True)
    snap = system.sample()
    assert snap["net_info"] is not None and snap["disk_info"] is not None
    system.set_detailed(False)
    assert system.sample()["net_info"] is None


def test_a_slow_sample_slows_the_pace_without_gaps(sampler, monkeypatch):
    import time

    from gi.repository import GLib

    sampler.config.set("interval_ms", 100)
    real_work = sampler._work

    def slow_work():
        time.sleep(0.13)  # longer than the interval: the timer skips every other tick
        return real_work()

    monkeypatch.setattr(sampler, "_work", slow_work)
    loop = GLib.MainLoop()
    sampler.start()
    GLib.timeout_add(1500, loop.quit)
    loop.run()
    GLib.source_remove(sampler._timer)
    times = list(sampler.history.times)
    assert len(times) >= 4
    assert not any(t.gap for t in times)  # before: every sample after the first was a gap
    assert all(t.interval >= 0.2 for t in times[1:])
