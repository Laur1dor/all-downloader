"""The CAPTCHA audio worker remains offline and rejects oversized media."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np = None

from scripts import ig_audio_transcribe as worker


def _fake_av(frame_count: int, decoded: list[int] | None = None):
    class Frame:
        samples = worker.SAMPLE_RATE

        def to_ndarray(self):
            return SimpleNamespace(tobytes=lambda: b"\0\0" * self.samples)

    class Packet:
        def decode(self):
            if decoded is not None:
                decoded.append(1)
            return [Frame()]

    class Container:
        def __init__(self):
            self.streams = [SimpleNamespace(type="audio")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def demux(self, _stream):
            for _ in range(frame_count):
                yield Packet()

    class Resampler:
        def __init__(self, **_kwargs):
            pass

        def resample(self, frame):
            return [frame] if frame is not None else []

    return SimpleNamespace(
        open=lambda *_args, **_kwargs: Container(),
        audio=SimpleNamespace(
            resampler=SimpleNamespace(AudioResampler=Resampler),
        ),
    )


class AudioTranscribeTests(unittest.TestCase):
    def test_rejects_input_over_three_mib_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp3"
            with path.open("wb") as handle:
                handle.seek(worker.MAX_INPUT_BYTES)
                handle.write(b"x")
            with (
                patch.object(worker, "_decode_bounded") as decode,
                self.assertRaisesRegex(worker.TranscriptionError, "input_too_large"),
            ):
                worker.transcribe(path, Path(directory))
            decode.assert_not_called()

    def test_rejects_more_than_thirty_decoded_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp3"
            path.write_bytes(b"compressed")
            decoded: list[int] = []
            with (
                patch.dict(sys.modules, {"av": _fake_av(10_000, decoded)}),
                self.assertRaisesRegex(worker.TranscriptionError, "audio_too_long"),
            ):
                worker.transcribe(path, Path(directory))
            self.assertEqual(len(decoded), 31)

    @unittest.skipIf(np is None, "ASR dependency is installed in iglogin, not bot")
    def test_uses_only_local_tiny_model_and_clean_json_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "clip.mp3"
            path.write_bytes(b"compressed")
            models = root / "models"
            models.mkdir()
            seen = {}

            class FakeModel:
                def __init__(self, name, **kwargs):
                    seen["name"] = name
                    seen["kwargs"] = kwargs
                    print("model startup noise")

                def transcribe(self, audio, **kwargs):
                    seen["audio_type"] = type(audio)
                    seen["audio_dtype"] = audio.dtype
                    seen["audio_samples"] = audio.size
                    seen["audio_amplitude"] = float(audio.max())
                    seen["transcribe_kwargs"] = kwargs
                    return iter([SimpleNamespace(text="  five six  ")]), None

            output = io.StringIO()
            with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeModel)}), \
                 patch.dict(sys.modules, {"av": _fake_av(1)}), \
                 patch.dict(os.environ, {"IG_CAPTCHA_MODEL_DIR": str(models)}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(worker.main([str(path)]), 0)

            self.assertEqual(output.getvalue(), '{"text": "five six"}\n')
            self.assertEqual(seen["name"], "tiny.en")
            self.assertTrue(seen["kwargs"]["local_files_only"])
            self.assertEqual(seen["kwargs"]["download_root"], str(models))
            self.assertEqual(seen["kwargs"]["compute_type"], "int8")
            self.assertEqual(seen["kwargs"]["cpu_threads"], 2)
            self.assertEqual(seen["kwargs"]["num_workers"], 1)
            self.assertEqual(seen["transcribe_kwargs"]["beam_size"], 1)
            self.assertIs(seen["audio_type"], np.ndarray)
            self.assertEqual(seen["audio_dtype"], np.dtype("float32"))
            self.assertEqual(seen["audio_samples"], worker.SAMPLE_RATE)
            self.assertEqual(seen["audio_amplitude"], 0.0)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")


if __name__ == "__main__":
    unittest.main()
