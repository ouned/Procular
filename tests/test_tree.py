import os

import pytest

pytest.importorskip("gi")

from procular.config import Config  # noqa: E402
from procular.models.process_tree import ProcessTree  # noqa: E402


def mk(pid, ppid, start=1, **kw):
    rec = {"pid": pid, "ppid": ppid, "starttime": start, "name": f"p{pid}", "uid": os.getuid(),
           "euid": os.getuid(), "state": "S", "capeff": 0, "kthread": False, "nice": 0}
    rec.update(kw)
    return rec


@pytest.fixture
def tree(tmp_path):
    cfg = Config(path=str(tmp_path / "settings.json"))
    cfg.data["highlight_seconds"] = 0  # immediate removal unless overridden
    return ProcessTree(cfg)


def children(item):
    return [item.children.get_item(i).pid for i in range(item.children.get_n_items())]


def test_initial_build_and_hierarchy(tree):
    tree.apply({1: mk(1, 0), 10: mk(10, 1), 11: mk(11, 10), 2: mk(2, 0)})
    assert tree.root.get_n_items() == 2
    assert children(tree.get(1)) == [10]
    assert children(tree.get(10)) == [11]
    assert tree.get(11).parent is tree.get(10)
    assert tree.get(10).hl == f"uid{os.getuid()}"  # coloured by user; not "new" on first load


def test_new_process_highlight_and_reparent(tree):
    tree.config.data["highlight_seconds"] = 5
    tree.apply({1: mk(1, 0), 10: mk(10, 1)})
    tree.apply({1: mk(1, 0), 10: mk(10, 1), 20: mk(20, 10)})
    assert tree.get(20).hl == "new"
    assert children(tree.get(10)) == [20]
    # parent exits: kernel reparents child to 1
    tree.apply({1: mk(1, 0), 20: mk(20, 1)})
    assert tree.get(10).hl == "term"           # kept, highlighted red
    assert tree.get(20).parent is tree.get(1)
    assert 20 in children(tree.get(1))


def test_terminated_removed_after_duration(tree):
    tree.apply({1: mk(1, 0), 10: mk(10, 1)})
    tree.apply({1: mk(1, 0)})
    assert tree.get(10) is None
    assert children(tree.get(1)) == []
    assert tree.flat.get_n_items() == 1


def test_pid_reuse_creates_new_item(tree):
    tree.apply({1: mk(1, 0), 10: mk(10, 1, start=5)})
    old = tree.get(10)
    tree.apply({1: mk(1, 0), 10: mk(10, 1, start=99)})
    assert tree.get(10) is not old
    assert tree.flat.get_n_items() == 2


def test_exit_history_records_exits_once(tree):
    tree.config.data["highlight_seconds"] = 5  # terminated items linger, must not be recorded twice
    seen = []
    tree.connect("exited", lambda _t, recs: seen.extend(recs))
    tree.apply({1: mk(1, 0), 10: mk(10, 1, cpu=3.0, rss=1000, create_time=100.0)})
    tree.apply({1: mk(1, 0), 10: mk(10, 1, cpu=7.0, rss=500, create_time=100.0)})
    tree.apply({1: mk(1, 0)})
    tree.apply({1: mk(1, 0)})
    assert [r["pid"] for r in seen] == [10]
    rec = seen[0]
    assert rec["name"] == "p10"
    assert rec["parent"] == "p1 (1)"
    assert rec["peak_cpu"] == 7.0
    assert rec["peak_rss"] == 1000
    assert rec["runtime"] > 0
    assert list(tree.exit_history) == seen


def test_exit_history_pid_reuse_and_zombie_status(tree):
    tree.apply({1: mk(1, 0), 10: mk(10, 1, start=5)})
    tree.apply({1: mk(1, 0), 10: mk(10, 1, start=5, state="Z", exit_code=2 << 8)})
    tree.apply({1: mk(1, 0), 10: mk(10, 1, start=9)})  # reaped and PID reused
    assert len(tree.exit_history) == 1
    assert tree.exit_history[0]["status"] == "Exited with 2"


def test_exit_history_skips_kernel_threads_and_first_load(tree):
    tree.config.data["show_kernel_threads"] = False
    tree.apply({1: mk(1, 0), 2: mk(2, 0, kthread=True), 30: mk(30, 2, kthread=True)})
    tree.apply({1: mk(1, 0), 2: mk(2, 0, kthread=True)})
    assert len(tree.exit_history) == 0


def test_sparkline_history(tree):
    for cpu in (1.0, 2.0, 3.0):
        tree.apply({1: mk(1, 0, cpu=cpu, rss=10)})
    assert list(tree.get(1).cpu_hist) == [1.0, 2.0, 3.0]
    assert list(tree.get(1).mem_hist) == [10, 10, 10]


def test_user_colours_are_stored(tmp_path):
    path = tmp_path / "settings.json"
    cfg = Config(path=str(path))
    changed = []
    cfg.connect("changed", lambda _c, key: changed.append(key))
    cfg.set_user_color(0, "#112233")
    cfg.set_user_color(0, "#112233")  # no change
    cfg.save_now()
    assert changed == ["user_colors"]
    assert Config(path=str(path)).get("user_colors") == {"0": "#112233"}
    cfg.reset_user_colors()
    assert cfg.get("user_colors") == {}


def _signals(item):
    seen = []
    item.connect("updated", lambda _i: seen.append("updated"))
    item.connect("history", lambda _i: seen.append("history"))
    return seen


def test_only_changed_processes_are_refreshed(tree):
    recs = lambda cpu: {1: mk(1, 0, cpu=0.0, rss=10), 10: mk(10, 0, cpu=cpu, rss=10)}  # noqa: E731
    tree.apply(recs(0.0))
    idle, busy = _signals(tree.get(1)), _signals(tree.get(10))
    tree.apply(recs(5.0))
    assert "updated" in busy and "history" in busy
    assert "updated" not in idle  # identical record: its cells are left alone
    # the scanner hands out the very same dict for an idle process
    same = recs(5.0)
    tree.apply(same)
    del busy[:]
    tree.apply(same)
    assert "updated" not in busy
    assert "history" in busy  # ... but the sample from before still scrolls through the graph


def test_history_signal_stops_once_the_graph_is_flat(tree):
    from procular.models.process_tree import SPARK_SAMPLES
    rec = {1: mk(1, 0, cpu=0.0, rss=10)}
    for _ in range(SPARK_SAMPLES + 2):
        tree.apply(rec)
    seen = _signals(tree.get(1))
    tree.apply(rec)
    assert seen == []


def test_rows_are_coloured_by_user(tree):
    seen = []
    tree.connect("users-changed", lambda _t: seen.append(set(tree.users)))
    me = os.getuid()
    tree.apply({1: mk(1, 0, uid=0, euid=0), 10: mk(10, 1), 11: mk(11, 1, state="T"), 12: mk(12, 1, uid=None)})
    assert tree.get(1).hl == "uid0"
    assert tree.get(10).hl == f"uid{me}"
    assert tree.get(11).hl == "stopped"  # states win over the user colour
    assert tree.get(12).hl == ""  # owner unknown
    assert seen == [{0, me}]
    # a user that was not there before: the colours have to be extended
    tree.apply({1: mk(1, 0, uid=0, euid=0), 10: mk(10, 1), 20: mk(20, 1, uid=me + 1, euid=me + 1)})
    assert tree.get(20).hl == f"uid{me + 1}"
    assert seen[-1] == {0, me, me + 1} and len(seen) == 2


def test_colour_mode_switch(tree):
    rec = {1: mk(1, 0), 10: mk(10, 1, state="Z")}
    tree.apply(rec)
    seen = _signals(tree.get(1))
    tree.config.data["highlight_mode"] = "category"
    tree.refresh_highlights()
    assert tree.get(1).hl == "own" and seen == ["updated"]
    tree.config.data["highlight_mode"] = "none"
    tree.apply(rec)  # picked up without refresh_highlights(), too
    assert tree.get(1).hl == ""
    assert tree.get(10).hl == "zombie"


def test_highlight_change_refreshes_unchanged_process(tree):
    tree.config.data["highlight_seconds"] = 0
    tree.config.data["highlight_mode"] = "category"
    rec = {1: mk(1, 0), 10: mk(10, 1)}
    tree.apply(rec)
    seen = _signals(tree.get(10))
    tree.config.data["highlights"]["own"]["enabled"] = False
    tree.apply(rec)
    assert "updated" in seen and tree.get(10).hl != "own"


def test_subtree_sums(tree):
    tree.config.data["highlight_seconds"] = 5
    tree.apply({1: mk(1, 0, cpu=1.0, rss=10, io_total=None), 10: mk(10, 1, cpu=2.0, rss=20, io_total=5),
                11: mk(11, 10, cpu=3.0, rss=30, io_total=None)})
    assert tree.get(11).sums is None  # no children: the row shows its own values
    assert tree.get(10).sums["cpu"] == 5.0
    assert tree.get(10).sums["io_total"] == 5
    assert tree.get(1).sums["rss"] == 60
    assert tree.get(1).sums["io_total"] == 5  # unreadable values are left out
    assert list(tree.get(1).sum_cpu_hist) == [6.0]
    # a parent whose child's usage changed is refreshed
    seen = _signals(tree.get(1))
    tree.apply({1: mk(1, 0, cpu=1.0, rss=10, io_total=None), 10: mk(10, 1, cpu=2.0, rss=20, io_total=5),
                11: mk(11, 10, cpu=4.0, rss=30, io_total=None)})
    assert "updated" in seen and "history" in seen
    assert list(tree.get(1).sum_cpu_hist) == [6.0, 7.0]
    # exited processes no longer count; without children the sums are gone
    tree.apply({1: mk(1, 0, cpu=1.0, rss=10), 10: mk(10, 1, cpu=2.0, rss=20)})
    assert tree.get(11).terminated
    assert tree.get(10).sums["cpu"] == 2.0
    assert tree.get(1).sums["cpu"] == 3.0
    tree.config.data["highlight_seconds"] = 0
    tree.apply({1: mk(1, 0, cpu=1.0, rss=10)})
    assert tree.get(1).sums is None


def test_subtree_sums_only_add_up_changed_branches(tree, monkeypatch):
    recs = {1: mk(1, 0, cpu=1.0), 10: mk(10, 1, cpu=2.0), 11: mk(11, 10, cpu=3.0),
            20: mk(20, 1, cpu=4.0), 21: mk(21, 20, cpu=5.0)}
    tree.apply(recs)
    assert tree.get(1).sums["cpu"] == 15.0
    added_up = []
    real = ProcessTree._subtree

    def spy(self, item, dirty):
        added_up.append(item.pid)
        return real(self, item, dirty)

    monkeypatch.setattr(ProcessTree, "_subtree", spy)
    recs = {**recs, 21: mk(21, 20, cpu=6.0)}  # only the second branch is busy
    tree.apply(recs)
    assert 10 not in added_up and 11 not in added_up
    assert tree.get(20).sums["cpu"] == 10.0 and tree.get(1).sums["cpu"] == 16.0
    assert list(tree.get(10).sum_cpu_hist) == [5.0, 5.0]  # the idle branch's history still moves on
    # 11 moves over to 20 (re-parented); a new child appears under the idle 10
    recs = {**recs, 11: mk(11, 20, cpu=3.0), 12: mk(12, 10, cpu=0.5)}
    tree.apply(recs)
    assert tree.get(10).sums["cpu"] == 2.5
    assert tree.get(20).sums["cpu"] == 13.0
    assert tree.get(1).sums["cpu"] == 16.5


def test_parent_cycles_are_broken(tree):
    # initial bulk insert with a 2-cycle (e.g. mid-scan PID reuse)
    tree.apply({10: mk(10, 20, cpu=1.0), 20: mk(20, 10, cpu=2.0)})
    assert len(list(tree.get(10).ancestors())) <= 1
    assert len(list(tree.get(20).ancestors())) <= 1
    assert tree.root.get_n_items() == 1
    # live reparenting that would turn a parent into a child of its own descendant
    root = tree.root.get_item(0)
    child = root.children.get_item(0)
    tree.apply({root.pid: mk(root.pid, child.pid, cpu=1.0), child.pid: mk(child.pid, root.pid, cpu=2.0)})
    assert len(list(tree.get(root.pid).ancestors())) <= 1
    assert len(list(tree.get(child.pid).ancestors())) <= 1
