"""One OS lock for every process that opens the Instagram Chromium profile.

The lock file is never removed: unlinking it while another process owns it
would let a third process lock a different inode and open the same profile.
"""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


class ProfileBusy(TimeoutError):
    """Another helper currently owns the persistent Chromium profile."""


def _lock_one_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_one_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def profile_lock(timeout: float = 20, path: Path | None = None) -> Iterator[None]:
    """Hold the profile across browser launch, use, cookie write and close."""
    lock_path = Path(path or os.getenv("IG_BROWSER_LOCK", "/app/data/ig-browser.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)
    with lock_path.open("a+b") as handle:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                _lock_one_byte(handle)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN) and getattr(
                    exc, "winerror", None
                ) not in (33, 36):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProfileBusy("Instagram browser profile is busy") from exc
                time.sleep(min(0.1, remaining))
        try:
            yield
        finally:
            _unlock_one_byte(handle)
