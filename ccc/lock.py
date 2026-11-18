"""Single-instance lock: only one copy of the app may send at a time."""

from __future__ import annotations

import os


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path):
        self.path = str(path)
        self._fh = None

    def acquire(self):
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning("Another Canvas Command Center window is already open.") from None
        return self

    def release(self):
        if self._fh:
            self._fh.close()  # closing the handle releases the lock
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
