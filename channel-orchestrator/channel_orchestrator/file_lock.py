from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_thread_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _registry_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    """Exclusive lock for a JSON store shared by threads and by orch-serve / orch-scheduler.

    Callers must re-read the file inside the lock; an in-memory copy may be stale.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(str(lock_path.resolve())):
        with lock_path.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
