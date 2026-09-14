"""A process lock shared by local sync commands, released automatically on exit."""

import os
import threading
from contextlib import contextmanager

from codexauth import store


class SyncBusyError(Exception):
    pass


_thread_lock = threading.RLock()
_state = threading.local()


@contextmanager
def sync_lock():
    if not _thread_lock.acquire(blocking=False):
        raise SyncBusyError("Another local sync is running; this run was skipped.")
    try:
        if getattr(_state, "held", False):
            yield
            return
        store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(store.STORE_DIR / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            if os.name == "nt":
                import msvcrt
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise SyncBusyError("Another local sync is running; this run was skipped.") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise SyncBusyError("Another local sync is running; this run was skipped.") from exc
            _state.held = True
            try:
                yield
            finally:
                _state.held = False
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        _thread_lock.release()
