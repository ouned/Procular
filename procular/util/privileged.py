"""Run the privileged helper through pkexec (polkit authentication dialog)."""

import base64
import json
import os
import sys

from gi.repository import Gio, GLib

# The .deb/.rpm package's copy first: it always matches the app, while one installed by
# `sudo ./install.sh --helper` may be older (and the package replaces its polkit policy).
INSTALLED_HELPERS = ("/usr/libexec/procular-helper", "/usr/local/libexec/procular-helper")
SOURCE_HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "helper", "procular_helper.py")


def helper_command(installed=INSTALLED_HELPERS):
    for helper in installed:
        if os.path.exists(helper) and os.access(helper, os.X_OK):
            return ["pkexec", helper]
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return ["pkexec", python, "-I", SOURCE_HELPER]


def run(args, callback):
    """Run helper with args; callback(result, error_message) on the main loop."""
    argv = helper_command() + [str(a) for a in args]
    try:
        proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as err:
        callback(None, f"Could not start pkexec: {err.message}")
        return

    def done(p, res):
        try:
            ok, out, err = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            callback(None, e.message)
            return
        status = p.get_exit_status() if p.get_if_exited() else -1
        if status in (126, 127):
            callback(None, "Authorization was cancelled or denied")
            return
        try:
            payload = json.loads(out or "{}")
        except ValueError:
            callback(None, (err or out or "Helper failed").strip())
            return
        if not payload.get("ok"):
            error = payload.get("error") or (err or "Helper failed").strip()
            if error == "unknown operation" and argv[0] == "pkexec" and argv[1] in INSTALLED_HELPERS:
                # an installed copy is only replaced by installing it again (see install.sh)
                error = ("The installed Procular helper is too old for this. Update it "
                         "(sudo ./install.sh --helper, or the package) and try again.")
            callback(None, error)
            return
        callback(payload.get("result"), None)

    proc.communicate_utf8_async(None, None, done)


def run_for_processes(op, targets, args, callback):
    """Send identity-checked targets through pkexec, including when only one was selected."""
    target_arg = ",".join(f"{pid}@{starttime}" for pid, starttime in targets)

    def done(result, error):
        if error and error.startswith("invalid pid"):
            # Older installed helpers only accept numeric PIDs. Do not retry with those:
            # the original process could disappear while the authentication dialog is open.
            error = "The installed Procular helper is too old to check process identity. Update it and try again."
        callback(result, error)

    run([op, target_arg, *args], done)


def decode_file(result):
    """Helper 'read' results carry base64 encoded file content."""
    return base64.b64decode(result.get("data", "")) if result else b""
