import threading

from procular.util import process_name


def _in_thread(fn):
    """Run fn on a thread of its own: PR_SET_NAME names only the calling thread, and the
    test process's main thread keeps its name."""
    out = {}

    def run():
        out["result"] = fn()
        with open("/proc/thread-self/comm") as fh:
            out["comm"] = fh.read().strip()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out


def test_set_name():
    assert _in_thread(lambda: process_name.set_name("procular")) == {"result": True, "comm": "procular"}


def test_long_names_are_cut_to_15_bytes():
    out = _in_thread(lambda: process_name.set_name("a-very-long-process-name"))
    assert out == {"result": True, "comm": "a-very-long-pro"}
