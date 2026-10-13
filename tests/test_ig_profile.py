"""Instagram helpers cannot open the same persistent Chromium profile together."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import iglogin_browser  # noqa: E402
import igremote  # noqa: E402
import igresolve  # noqa: E402
from ig_profile import ProfileBusy, profile_lock  # noqa: E402


class ProfileLockTests(unittest.TestCase):
    def test_one_owner_across_processes_and_release_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "ig-browser.lock"
            child = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from ig_profile import ProfileBusy, profile_lock; "
                "\ntry:\n with profile_lock(timeout=0, path=sys.argv[2]): "
                "print('acquired')\nexcept ProfileBusy: print('busy')"
            )

            def child_result():
                return subprocess.run(
                    [sys.executable, "-c", child, str(SCRIPTS), str(lock)],
                    capture_output=True, text=True, timeout=10, check=True,
                ).stdout.strip()

            with self.assertRaisesRegex(ValueError, "test failure"):
                with profile_lock(path=lock):
                    self.assertEqual(child_result(), "busy")
                    with self.assertRaises(ProfileBusy):
                        with profile_lock(timeout=0, path=lock):
                            pass
                    raise ValueError("test failure")
            self.assertEqual(child_result(), "acquired")
            self.assertTrue(lock.exists(), "the lock inode must not be removed")

    def test_helpers_return_busy_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            lock = folder / "ig-browser.lock"
            status = folder / "igremote_status.json"

            def immediate_lock(*, timeout):
                self.assertIn(timeout, (0, 20))
                return profile_lock(timeout=0, path=lock)

            with (
                patch.dict(os.environ, {"IG_BROWSER_LOCK": str(lock)}),
                patch("ig_profile.profile_lock", side_effect=immediate_lock),
                patch.object(igremote, "STATUS_FILE", status),
                patch.object(iglogin_browser, "_run_browser") as login_browser,
                patch.object(igresolve, "_resolve_with_browser") as resolver_browser,
                patch.object(igremote, "_session_with_browser") as remote_browser,
                profile_lock(path=lock),
            ):
                self.assertEqual(iglogin_browser.run(), 3)
                result = igresolve.resolve("https://www.instagram.com/reel/AbCd123/")
                self.assertTrue(result["busy"])
                igremote.session()
                self.assertEqual(json.loads(status.read_text())["state"], "failed")
                login_browser.assert_not_called()
                resolver_browser.assert_not_called()
                remote_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
