from procular.models import highlight


def rec(**kw):
    base = {"uid": 1000, "euid": 1000, "state": "S", "capeff": 0, "kthread": False, "nice": 0}
    base.update(kw)
    return base


def test_own_process():
    assert highlight.categories(rec(), my_uid=1000) == ["own"]


def test_stopped_wins():
    cats = highlight.categories(rec(state="T"), my_uid=1000)
    assert highlight.pick(cats, {"stopped", "own"}) == "stopped"


def test_service_before_elevated():
    r = rec(uid=985, euid=985, capeff=0x22, unit="systemd-oomd.service", unit_kind="service",
            unit_scope="system")
    cats = highlight.categories(r, my_uid=1000)
    assert cats.index("system_service") < cats.index("elevated")


def test_benign_caps_not_elevated():
    r = rec(capeff=1 << 35)
    assert "elevated" not in highlight.categories(r, my_uid=1000)


def test_setuid_is_elevated():
    r = rec(uid=1000, euid=0)
    assert "elevated" in highlight.categories(r, my_uid=1000)


def test_disabled_category_falls_through():
    cats = highlight.categories(rec(sandboxed="flatpak"), my_uid=1000)
    assert highlight.pick(cats, {"own"}) == "own"
    assert highlight.pick(cats, {"own", "sandboxed"}) == "sandboxed"


def test_kthread_and_other_user():
    assert highlight.categories(rec(uid=0, euid=0, kthread=True), my_uid=1000) == ["kthread"]
    assert "other_user" in highlight.categories(rec(uid=0, euid=0), my_uid=1000)


def test_state_highlight():
    assert highlight.state(rec(state="T"), {"stopped", "zombie"}) == "stopped"
    assert highlight.state(rec(state="Z"), {"stopped", "zombie"}) == "zombie"
    assert highlight.state(rec(state="Z"), {"stopped"}) == ""
    assert highlight.state(rec(), {"stopped", "zombie"}) == ""


def test_user_colours():
    colors = highlight.UserColors(my_uid=1000)
    assert colors.color(1000) == highlight.OWN_COLOR
    assert colors.color(0) == highlight.ROOT_COLOR
    assert highlight.UserColors(my_uid=0).color(0) == highlight.OWN_COLOR
    n = len(highlight.USER_PALETTE)
    first = colors.color(101)
    assert first == highlight.USER_PALETTE[101 % n]
    assert colors.color(101) == first  # a user keeps their colour
    # uids that point at the same palette entry still get different colours ...
    others = [colors.color(101 + n * i) for i in range(1, n)]
    assert len({first, *others}) == n
    # ... until the palette is used up
    assert colors.color(101 + n * n) in highlight.USER_PALETTE
    assert colors.color(101, {"101": "#123456"}) == "#123456"
    assert colors.color(101, {"102": "#123456"}) == first
    assert len({*highlight.USER_PALETTE, highlight.OWN_COLOR, highlight.ROOT_COLOR}) == n + 2
