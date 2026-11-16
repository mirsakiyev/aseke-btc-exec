"""Single-worker process lock (OS-level, released automatically if the process dies).

Windows uses ``msvcrt.locking``; POSIX uses ``fcntl.flock``. Every command that
can touch the simulated exchange or mutate execution state acquires it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class LockHeld(Exception):
    pass


class ProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh: IO[str] | None = None

    def acquire(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockHeld(f"another ASEKE Exec worker holds {self.path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
