"""Run blocking work in a thread pool and deliver results on the GLib main loop."""

import traceback
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="procular")


def run_async(func, callback=None, *args):
    """Run func(*args) in a worker; call callback(result, error) on the main thread."""

    def job():
        try:
            result = func(*args)
            error = None
        except Exception as exc:  # noqa: BLE001 - delivered to the callback
            result = None
            error = exc
            if not isinstance(exc, (OSError,)):
                traceback.print_exc()
        if callback is not None:
            GLib.idle_add(_deliver, callback, result, error)

    return _executor.submit(job)


def _deliver(callback, result, error):
    callback(result, error)
    return False
