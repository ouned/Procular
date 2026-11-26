"""Row highlight classification (pure logic, unit tested)."""

import os

MY_UID = os.getuid()

# Order matters: the first enabled matching category wins. "new" and "term"
# are handled by the tree because they are time based.
PRIORITY = [
    "new", "term", "stopped", "zombie", "container", "sandboxed", "system_service",
    "elevated", "user_service", "kthread", "own", "high_priority", "other_user",
]

# time or state based highlights: they apply in every colour mode and always win
STATES = ("new", "term", "stopped", "zombie")

# row colours by user: the current user and root have fixed colours, everybody else gets
# one from the palette (see UserColors)
OWN_COLOR = "#3584e4"
ROOT_COLOR = "#ff7800"
USER_PALETTE = ("#2190a4", "#e5a50a", "#d56199", "#986a44", "#6f5fd0", "#63a02c", "#33c7de", "#c061cb",
                "#b5cc18", "#7a8fa6", "#ef7a6d", "#cdb47b")

# capabilities that do not make a process "elevated" (CAP_WAKE_ALARM, CAP_BLOCK_SUSPEND)
BENIGN_CAPS = (1 << 35) | (1 << 36)


def categories(rec, my_uid=MY_UID):
    """Return all static/dynamic categories a process record belongs to, in priority order."""
    found = set()
    state = rec.get("state", "")
    if state in ("T", "t"):
        found.add("stopped")
    if state == "Z":
        found.add("zombie")
    uid = rec.get("uid")
    euid = rec.get("euid")
    if uid is not None and uid != 0 and (euid != uid or (rec.get("capeff", 0) & ~BENIGN_CAPS)):
        found.add("elevated")
    if rec.get("container"):
        found.add("container")
    if rec.get("sandboxed"):
        found.add("sandboxed")
    if rec.get("unit_kind") == "service":
        if rec.get("unit_scope") == "system" and not rec.get("unit", "").startswith("user@"):
            found.add("system_service")
        elif rec.get("unit_scope") == "user":
            found.add("user_service")
    if rec.get("kthread"):
        found.add("kthread")
    if uid == my_uid:
        found.add("own")
    if (rec.get("nice") or 0) < 0:
        found.add("high_priority")
    if uid != my_uid and not rec.get("kthread"):
        found.add("other_user")
    return [c for c in PRIORITY if c in found]


def pick(cats, enabled):
    for cat in cats:
        if cat in enabled:
            return cat
    return ""


def state(rec, enabled):
    """The enabled state highlight ("stopped" / "zombie") of a process record, or ""."""
    st = rec.get("state", "")
    if st in ("T", "t"):
        return "stopped" if "stopped" in enabled else ""
    if st == "Z" and "zombie" in enabled:
        return "zombie"
    return ""


def user_class(uid):
    return f"uid{uid}"


class UserColors:
    """Gives every user a row colour.

    Other users than the current one and root share a palette: a user gets the colour its uid
    points at, or, while another user has that one, the next colour that is used the least.
    """

    def __init__(self, my_uid=MY_UID):
        self.my_uid = my_uid
        self._slots = {}  # uid -> index into USER_PALETTE
        self._used = [0] * len(USER_PALETTE)

    def default(self, uid):
        if uid == self.my_uid:
            return OWN_COLOR
        if uid == 0:
            return ROOT_COLOR
        slot = self._slots.get(uid)
        if slot is None:
            n = len(USER_PALETTE)
            slot = min(((uid + i) % n for i in range(n)), key=self._used.__getitem__)
            self._slots[uid] = slot
            self._used[slot] += 1
        return USER_PALETTE[slot]

    def color(self, uid, overrides=None):
        """overrides maps str(uid) to a colour the user picked."""
        return (overrides.get(str(uid)) if overrides else None) or self.default(uid)
