"""Records of processes that exited while Procular was running (pure logic, unit tested)."""

import signal

_SIGNAL_NAMES = {s.value: s.name for s in signal.Signals}


def exit_status_text(code):
    """Decode a wait(2)-style status as found in /proc/<pid>/stat field 52."""
    if code is None:
        return ""
    sig = code & 0x7F
    if sig:
        text = f"Killed by {_SIGNAL_NAMES.get(sig, f'signal {sig}')}"
        if code & 0x80:
            text += " (core dumped)"
        return text
    return f"Exited with {(code >> 8) & 0xFF}"


def exit_failed(code):
    """True when the status means failure (non-zero exit or killed by a signal)."""
    return code is not None and code != 0


def make_exit_record(item, exit_time, seq):
    """Snapshot what we knew about a process at the moment it disappeared."""
    rec = item.rec
    parent = item.parent
    create = rec.get("create_time") or 0
    code = rec.get("exit_code") if rec.get("state") == "Z" else None
    return {
        "seq": seq,
        "pid": item.pid,
        "name": rec.get("name") or rec.get("comm") or "",
        "ppid": rec.get("ppid"),
        "parent": f"{parent.name} ({parent.pid})" if parent is not None else (
            str(rec.get("ppid")) if rec.get("ppid") else ""),
        "user": rec.get("user") or "",
        "uid": rec.get("uid"),
        "create_time": create,
        "exit_time": exit_time,
        "runtime": max(0.0, exit_time - create) if create else None,
        "exit_code": code,
        "status": exit_status_text(code),
        "cpu_time": rec.get("cpu_time"),
        "peak_cpu": item.peak_cpu,
        "peak_rss": item.peak_rss,
        "unit": rec.get("unit") or "",
        "exe": rec.get("exe") or "",
        "cmdline": rec.get("cmdline") or "",
        "kthread": bool(rec.get("kthread")),
        "icon": item.icon,
    }
