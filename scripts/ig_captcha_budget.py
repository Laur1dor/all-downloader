"""Persistent, fail-closed budget for Instagram CAPTCHA handler invocations.

The caller must already hold the shared Instagram profile lock, which also
serializes reservations. No CAPTCHA content or credentials are stored here.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

_MAX_ATTEMPTS = 4
_WINDOW_SECONDS = 24 * 60 * 60
_MIN_INTERVAL_SECONDS = 600
_MAX_STATE_BYTES = 4096


def _budget_file() -> Path:
    profile = Path(os.getenv("IG_PROFILE_DIR", "/app/data/ig-profile"))
    return profile.parent / "ig-captcha-budget.json"


def reserve_attempt() -> bool:
    """Reserve one handler invocation; deny on limit or unreadable state.

    A reservation is written before the caller runs the handler, so a crash
    cannot make that attempt disappear. The caller owns the cross-process lock.
    """
    path = _budget_file()
    now = time.time()
    if not math.isfinite(now) or now < 0:
        return False

    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            return False
        saved = json.loads(raw)
        values = saved["attempts"]
        if not isinstance(values, list) or len(values) > _MAX_ATTEMPTS:
            return False
        attempts = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            try:
                stamp = float(value)
            except (OverflowError, ValueError):
                return False
            if not math.isfinite(stamp) or stamp < 0:
                return False
            attempts.append(stamp)
    except FileNotFoundError:
        attempts = []
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False

    attempts = [stamp for stamp in attempts if now - stamp < _WINDOW_SECONDS]
    if len(attempts) >= _MAX_ATTEMPTS:
        return False
    if attempts and now - max(attempts) < _MIN_INTERVAL_SECONDS:
        return False

    attempts.append(now)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"attempts": attempts}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError:
        return False
    return True
