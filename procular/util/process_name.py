"""The name process lists show for Procular itself (the kernel's comm).

Without it that would be "python3" when run from the source tree (the binary that was
executed). PR_SET_NAME names only the calling thread, and the main thread's name is the
process name, so set_name() has to be called on the main thread. GLib's own threads (gmain,
gdbus, ...) name themselves.
"""

import ctypes
import os

PR_SET_NAME = 15
COMM_LEN = 15  # TASK_COMM_LEN without the terminating NUL


def set_name(name):
    """Set the calling thread's comm, cut to 15 bytes as the kernel would. False if that failed."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_NAME, ctypes.c_char_p(os.fsencode(name)[:COMM_LEN]), 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False  # cosmetic only
