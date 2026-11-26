import signal

from procular.models.process_history import exit_failed, exit_status_text


def test_exit_status_text():
    assert exit_status_text(None) == ""
    assert exit_status_text(0) == "Exited with 0"
    assert exit_status_text(1 << 8) == "Exited with 1"
    assert exit_status_text(signal.SIGKILL) == "Killed by SIGKILL"
    assert exit_status_text(signal.SIGSEGV | 0x80) == "Killed by SIGSEGV (core dumped)"


def test_exit_failed():
    assert not exit_failed(None)
    assert not exit_failed(0)
    assert exit_failed(1 << 8)
    assert exit_failed(signal.SIGTERM)
