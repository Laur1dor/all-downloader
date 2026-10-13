"""Checks for the bounded local CAPTCHA adapter without a live account."""

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import igcaptcha
import iglogin_browser as login


class CaptchaTests(unittest.TestCase):
    def setUp(self):
        budget = patch("ig_captcha_budget.reserve_attempt", return_value=True)
        budget.start()
        self.addCleanup(budget.stop)
        native = patch("ig_native_captcha.present", return_value=False)
        native.start()
        self.addCleanup(native.stop)

    def test_audio_origin_and_size(self):
        for url in (
            "http://www.google.com/recaptcha/audio",
            "https://www.google.com.evil.test/recaptcha/audio",
            "https://user@www.google.com/recaptcha/audio",
            "https://www.google.com:8443/recaptcha/audio",
            "https://www.google.com/elsewhere",
            "https://127.0.0.1/recaptcha/audio",
        ):
            self.assertFalse(igcaptcha.trusted_audio(url))
        frame = Mock()
        frame.evaluate.return_value = base64.b64encode(b"clip").decode()
        url = "https://www.google.com/recaptcha/api2/payload?p=secret"
        self.assertEqual(igcaptcha._audio(frame, url), b"clip")
        frame.evaluate.return_value = "x" * (igcaptcha.MAX_BYTES * 2)
        with self.assertRaises(ValueError):
            igcaptcha._audio(frame, url)
        self.assertIn("reader.cancel()", frame.evaluate.call_args.args[0])

    @patch("igcaptcha._transcribe", return_value="test answer")
    @patch("igcaptcha._audio", return_value=b"clip")
    @patch("igcaptcha._frame")
    @patch("igcaptcha._checked", return_value=False)
    def test_stops_after_two_unverified_attempts(
        self, checked, frames, audio, transcribe
    ):
        page, frame = Mock(), Mock()
        frames.side_effect = lambda page, kind: frame if kind == "bframe" else None
        self.assertFalse(igcaptcha.solve(page, Mock()))
        self.assertEqual(transcribe.call_count, 2)
        self.assertEqual(audio.call_count, 2)

    @patch("igcaptcha._transcribe", return_value="test answer")
    @patch("igcaptcha._audio", return_value=b"clip")
    @patch("igcaptcha._frame")
    @patch("igcaptcha._checked", side_effect=[False, True])
    def test_success_requires_verified_checkbox(
        self, checked, frames, audio, transcribe
    ):
        frame = Mock()
        frames.side_effect = lambda page, kind: frame if kind == "bframe" else None
        self.assertTrue(igcaptcha.solve(Mock(), Mock()))
        self.assertEqual(transcribe.call_count, 1)

    @patch("igcaptcha.subprocess.run")
    def test_worker_has_parent_timeout(self, run):
        run.return_value = Mock(returncode=0, stdout='{"text":"test"}')
        self.assertEqual(igcaptcha._transcribe(b"clip", 12), "test")
        self.assertEqual(run.call_args.kwargs["timeout"], 12)

    @patch("igcaptcha.present", return_value=False)
    @patch.object(login, "_authentication_state", return_value=False)
    @patch.object(login, "_first_visible")
    def test_login_fills_username_and_submits_only_once(self, first, auth, present):
        page, password, username = Mock(), Mock(), Mock()
        page.url = "https://www.instagram.com/accounts/login/"
        first.side_effect = lambda page, fields: (
            password if fields == login._PASS_FIELDS else username
        )
        with patch.dict("os.environ", {"IG_USERNAME": "owner"}):
            self.assertFalse(login._settle(page, "password"))
        username.fill.assert_called_once_with("owner")
        password.press.assert_called_once_with("Enter")

    def test_inconclusive_auth_is_not_logged_out(self):
        page = Mock()
        page.context.cookies.return_value = [{"name": "sessionid", "value": "private"}]
        for result in (
            {"status": 401},
            {"status": 403},
            {"status": 429},
            {"status": 200, "unknown": True},
            {"status": 503},
        ):
            page.evaluate.return_value = result
            self.assertIsNone(login._authentication_state(page))
        page.evaluate.return_value = {"status": 200, "user": "owner"}
        self.assertTrue(login._authentication_state(page))

    @patch("igcaptcha._checked", return_value=False)
    def test_budget_prevents_challenge_clicks(self, checked):
        page = Mock()
        with patch("ig_captcha_budget.reserve_attempt", return_value=False):
            self.assertFalse(igcaptcha.solve(page, Mock()))
        page.wait_for_timeout.assert_not_called()

    @patch("igcaptcha.present", return_value=True)
    @patch("igcaptcha.solve", return_value=False)
    @patch.object(login, "_authentication_state", return_value=None)
    def test_checkpoint_is_handled_even_when_auth_probe_unknown(
        self, auth, solve, present
    ):
        page = Mock(url="https://www.instagram.com/challenge/")
        page.locator.return_value.count.return_value = 0
        login._clear_checkpoint(page, 0)
        solve.assert_called_once()

    @patch.object(login, "code_from_mail")
    @patch.object(login, "_atomic_write")
    def test_mobile_number_is_not_treated_as_email_otp(self, write, mail):
        page = Mock(url="https://www.instagram.com/accounts/suspended/")
        page.get_by_text.return_value.count.return_value = 0
        page.inner_text.return_value = "Enter your mobile number"
        login._clear_checkpoint(page, 0)
        mail.assert_not_called()
        self.assertIn('phone_required', write.call_args.args[1])

    @patch.object(login, "_atomic_write")
    def test_native_human_check_is_not_declared_account_suspension(self, write):
        page = Mock(url="https://www.instagram.com/accounts/suspended/")
        self.assertFalse(login._restricted(page))
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
