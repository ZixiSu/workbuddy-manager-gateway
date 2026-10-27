"""Local paths and a single worker lock, outside publishable source by default."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path


def state_directory() -> Path:
    configured = os.environ.get("BUDDYGATE_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "state")))
    return base / "WorkBuddyManagerGateway" / "state"


@contextlib.contextmanager
def worker_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "worker.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
