"""CAPTCHA work has a restart-safe, bounded reservation budget."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ig_captcha_budget as budget


class CaptchaBudgetTests(unittest.TestCase):
    def test_interval_daily_cap_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "ig-profile"
            clock = SimpleNamespace(now=1000.0)
            with patch.dict(os.environ, {"IG_PROFILE_DIR": str(profile)}):
                with patch.object(budget, "time", SimpleNamespace(time=lambda: clock.now)):
                    self.assertTrue(budget.reserve_attempt())
                    clock.now = 1599.0
                    self.assertFalse(budget.reserve_attempt())

                # Reload drops module memory, as a fresh helper process would.
                importlib.reload(budget)
                with patch.object(budget, "time", SimpleNamespace(time=lambda: clock.now)):
                    self.assertFalse(budget.reserve_attempt())
                    for stamp in (1600.0, 2200.0, 2800.0):
                        clock.now = stamp
                        self.assertTrue(budget.reserve_attempt())
                    clock.now = 3400.0
                    self.assertFalse(budget.reserve_attempt())
                    clock.now = 1000.0 + 24 * 3600
                    self.assertTrue(budget.reserve_attempt())

            path = Path(directory) / "ig-captcha-budget.json"
            self.assertEqual(len(json.loads(path.read_text())["attempts"]), 4)

    def test_bad_state_and_io_failure_deny_without_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "ig-profile"
            path = Path(directory) / "ig-captcha-budget.json"
            with (
                patch.dict(os.environ, {"IG_PROFILE_DIR": str(profile)}),
                patch.object(budget, "time", SimpleNamespace(time=lambda: 1000.0)),
            ):
                for state in (
                    "{broken",
                    '{"attempts":[true]}',
                    '{"attempts":[NaN]}',
                    '{"attempts":[Infinity]}',
                    json.dumps({"attempts": [10**350]}),
                ):
                    path.write_text(state, encoding="utf-8")
                    self.assertFalse(budget.reserve_attempt(), state[:30])
                    self.assertEqual(path.read_text(encoding="utf-8"), state)

                path.unlink()
                with patch.object(Path, "replace", side_effect=OSError("disk full")):
                    self.assertFalse(budget.reserve_attempt())
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
