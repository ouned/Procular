import json

from procular.config import DEFAULTS, Config


def _config(tmp_path, stored=None):
    path = tmp_path / "settings.json"
    if stored is not None:
        path.write_text(stored if isinstance(stored, str) else json.dumps(stored))
    return Config(str(path))


def _reload(config):
    config.save_now()
    return Config(config.path)


def test_defaults_without_a_file(tmp_path):
    config = _config(tmp_path)
    assert config.data == DEFAULTS
    assert config.data is not DEFAULTS


def test_every_setting_survives_a_restart(tmp_path):
    config = _config(tmp_path)
    changed = {
        "color_scheme": "dark", "interval_ms": 2000, "background_slowdown": False, "highlight_seconds": 5, "confirm_kill": False,
        "show_kernel_threads": True, "show_other_users": False, "tree_mode": False,
        "highlight_mode": "category", "highlight_alpha": 0.4, "heat_cells": False, "binary_names": True,
        "columns": ["name", "cpu", "pid"], "column_widths": {"name": 300, "pid": 55},
        "window": {"width": 900, "height": 700, "maximized": True},
        "services_scope": "user", "services_only": False,
        "network_show_listening": False, "network_resolve_dns": False, "devices_show_all": True,
        "sysinfo_per_core": True,
        "history_samples": 600, "exit_history_size": 100, "about_music": False, "about_volume": 0.8,
        "about_track": "classic",
    }
    for key, value in changed.items():
        config.set(key, value)
    config.set_user_color(1000, "#112233")
    config.set_highlight("own", "color", "#445566")
    config.set_highlight("kthread", "enabled", False)
    config.set_sort("processes", {"column": "cpu", "descending": True})
    config.set_sort("history", None)
    # a test that does not cover a setting added later would pass without checking it
    covered = set(changed) | {"user_colors", "highlights", "sort"}
    assert covered == set(DEFAULTS)

    loaded = _reload(config)
    for key, value in changed.items():
        assert loaded.get(key) == value, key
    assert loaded.get("user_colors") == {"1000": "#112233"}
    assert loaded.get("highlights")["own"] == {"color": "#445566", "enabled": True}
    assert loaded.get("highlights")["kthread"]["enabled"] is False
    assert loaded.get("highlights")["new"] == DEFAULTS["highlights"]["new"]
    assert loaded.get("sort") == {"processes": {"column": "cpu", "descending": True}, "history": None}


def test_sort_unsorted_differs_from_never_sorted(tmp_path):
    config = _config(tmp_path)
    builtin = {"column": "name", "descending": False}
    assert config.get_sort("services", builtin) == builtin
    config.set_sort("services", None)  # the user removed the sort
    assert _reload(config).get_sort("services", builtin) is None


def test_set_sort_signals_only_changes(tmp_path):
    config = _config(tmp_path)
    seen = []
    config.connect("changed", lambda _c, key: seen.append(key))
    config.set_sort("processes", {"column": "cpu", "descending": True})
    config.set_sort("processes", {"column": "cpu", "descending": True})
    config.set_sort("processes", None)
    assert seen == ["sort", "sort"]


def test_values_of_the_wrong_type_are_ignored(tmp_path):
    config = _config(tmp_path, {
        "interval_ms": "fast", "tree_mode": 1, "window": None, "columns": "name", "sort": [],
        "column_widths": {"pid": "wide", "cpu": 77}, "user_colors": {"0": 5, "1000": "#112233"},
        "highlights": {"own": {"color": 3, "enabled": False}, "new": "red", "bogus": {"color": "#000000"}},
        "highlight_alpha": 1, "unknown_key": 1,
    })
    assert config.get("interval_ms") == DEFAULTS["interval_ms"]
    assert config.get("tree_mode") is True
    assert config.get("window") == DEFAULTS["window"]
    assert config.get("columns") == DEFAULTS["columns"]
    assert config.get("sort") == {}
    assert config.get("column_widths") == {"cpu": 77}
    assert config.get("user_colors") == {"1000": "#112233"}
    assert config.get("highlights")["own"] == {"color": DEFAULTS["highlights"]["own"]["color"], "enabled": False}
    assert config.get("highlights")["new"] == DEFAULTS["highlights"]["new"]
    assert "bogus" not in config.get("highlights")
    assert config.get("highlight_alpha") == 1
    assert "unknown_key" not in config.data


def test_unreadable_files_give_defaults(tmp_path):
    assert _config(tmp_path, "{ not json").data == DEFAULTS
    assert _config(tmp_path, "[1, 2]").data == DEFAULTS


def test_numbers_are_kept_in_range_and_in_their_type(tmp_path):
    # history_samples and exit_history_size have no control: they are edited by hand
    config = _config(tmp_path, {"history_samples": 600.0, "exit_history_size": -5, "interval_ms": 0,
                                "highlight_seconds": 2.6, "highlight_alpha": 7, "about_volume": -1})
    assert config.get("history_samples") == 600 and isinstance(config.get("history_samples"), int)
    assert config.get("exit_history_size") == 0
    assert config.get("interval_ms") == 100
    assert config.get("highlight_seconds") == 3 and isinstance(config.get("highlight_seconds"), int)
    assert config.get("highlight_alpha") == 1.0
    assert config.get("about_volume") == 0.0
    from collections import deque
    deque(maxlen=config.get("history_samples"))  # what the history does with it
    huge = _config(tmp_path, '{"history_samples": 1e400, "interval_ms": NaN}')
    assert huge.get("history_samples") == DEFAULTS["history_samples"]
    assert huge.get("interval_ms") == DEFAULTS["interval_ms"]


def test_unknown_choices_and_broken_entries_are_ignored(tmp_path):
    config = _config(tmp_path, {
        "color_scheme": "pink", "highlight_mode": "rainbow", "services_scope": "all",
        "window": {"width": "wide", "height": 700.0, "maximized": "yes"},
        "sort": {"processes": {"column": "cpu", "descending": True}, "services": "name", "history": None},
        "column_widths": {"pid": 55.0, "cpu": -3, "name": True},
        "user_colors": {"1000": "red; } * { color: red", "0": "#A1b2C3"},
        "highlights": {"own": {"color": "blue"}, "term": {"color": "#010203", "enabled": "no"}},
    })
    for key in ("color_scheme", "highlight_mode", "services_scope"):
        assert config.get(key) == DEFAULTS[key]
    assert config.get("window") == {"width": DEFAULTS["window"]["width"], "height": 700, "maximized": False}
    assert config.get("sort") == {"processes": {"column": "cpu", "descending": True}, "history": None}
    assert config.get("column_widths") == {"pid": 55, "cpu": 20}
    assert config.get("user_colors") == {"0": "#A1b2C3"}
    assert config.get("highlights")["own"] == DEFAULTS["highlights"]["own"]
    assert config.get("highlights")["term"] == {"color": "#010203", "enabled": True}


def test_duplicate_columns_are_deduplicated(tmp_path):
    config = _config(tmp_path, {"columns": ["name", "pid", "cpu", "pid", "name"]})
    assert config.get("columns") == ["name", "pid", "cpu"]


def test_relative_config_path_saves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Config("settings.json")
    config.set("interval_ms", 2000)
    config.save_now()
    assert (tmp_path / "settings.json").is_file()
    assert Config("settings.json").get("interval_ms") == 2000


def test_set_highlight_skips_unchanged_values(tmp_path):
    config = _config(tmp_path)
    seen = []
    config.connect("changed", lambda _c, key: seen.append(key))
    color = config.get("highlights")["own"]["color"]
    config.set_highlight("own", "color", color)
    assert seen == []
    config.set_highlight("own", "color", "#123456")
    assert seen == ["highlights"]


def test_a_damaged_file_is_kept_instead_of_overwritten(tmp_path, capsys):
    path = tmp_path / "settings.json"
    damaged = '{"interval_ms": 2000, "columns": ["name", "pid"], }'  # trailing comma
    config = _config(tmp_path, damaged)
    assert config.data == DEFAULTS
    assert (tmp_path / "settings.json.broken").read_text() == damaged
    assert "settings.json.broken" in capsys.readouterr().err
    config.save_now()
    assert json.loads(path.read_text()) == DEFAULTS
    assert (tmp_path / "settings.json.broken").read_text() == damaged


def test_deeply_nested_json_does_not_stop_the_start(tmp_path):
    assert _config(tmp_path, "[" * 200000 + "]" * 200000).data == DEFAULTS


def test_a_bom_is_accepted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"interval_ms": 2000}).encode())
    assert Config(str(path)).get("interval_ms") == 2000


def test_save_keeps_a_symlinked_file_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "procular.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    config = Config(str(link))
    config.set("interval_ms", 2000)
    config.save_now()
    assert link.is_symlink() and json.loads(real.read_text())["interval_ms"] == 2000
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
