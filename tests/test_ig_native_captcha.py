import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ig_native_captcha as native


class NativeCaptchaTests(unittest.TestCase):
    def test_clear_answers_only(self):
        self.assertEqual(native.normalise_answer("seven five three zero two"), "75302")
        self.assertEqual(native.normalise_answer("A B C D E"), "abcde")
        self.assertEqual(native.normalise_answer("53A8Z"), "53a8z")
        self.assertIsNone(native.normalise_answer("Please enter the numbers you hear"))
        self.assertIsNone(native.normalise_answer("1234567890"))

    @patch("igcaptcha._audio", return_value=b"audio")
    @patch("igcaptcha._transcribe", return_value="uncertain words")
    def test_ambiguous_audio_is_not_submitted(self, transcribe, audio):
        page = Mock()
        popup = (
            page.expect_popup.return_value.__enter__.return_value
            if hasattr(page.expect_popup.return_value, "__enter__")
            else Mock()
        )
        from contextlib import nullcontext

        page.expect_popup.return_value = nullcontext(Mock(value=popup))

        def register(event, callback):
            callback(
                Mock(url="https://www.facebook.com/captcha/tfbaudio/?secret=token")
            )

        page.context.on.side_effect = register
        self.assertFalse(
            native.solve(page, Mock(), __import__("time").monotonic() + 75)
        )
        page.locator.assert_not_called()
        page.context.remove_listener.assert_called_once()
        popup.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
