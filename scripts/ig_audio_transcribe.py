"""Offline, bounded transcription of one Instagram reCAPTCHA audio clip.

The caller must enforce a 45-second subprocess deadline. Model files must be
installed before this worker runs; it never downloads a model at runtime.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

MAX_INPUT_BYTES = 3 * 1024 * 1024
MAX_AUDIO_SECONDS = 30
SAMPLE_RATE = 16_000
DEFAULT_MODEL_DIR = Path("/app/data/ig-captcha-model")


class TranscriptionError(Exception):
    """A fixed error code safe for stderr; never includes paths or media."""


def _check_input(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TranscriptionError("input_unavailable") from exc
    if not path.is_file() or size <= 0:
        raise TranscriptionError("input_unavailable")
    if size > MAX_INPUT_BYTES:
        raise TranscriptionError("input_too_large")


def _decode_bounded(path: Path, wav_path: Path) -> None:
    try:
        import av

        samples = 0
        with (
            av.open(str(path), mode="r") as source,
            wave.open(str(wav_path), "wb") as output,
        ):
            audio_stream = next(
                (stream for stream in source.streams if stream.type == "audio"), None
            )
            if audio_stream is None:
                raise TranscriptionError("audio_decode_failed")
            resampler = av.audio.resampler.AudioResampler(
                format="s16",
                layout="mono",
                rate=SAMPLE_RATE,
            )
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            for packet in source.demux(audio_stream):
                for frame in packet.decode():
                    for converted in resampler.resample(frame):
                        samples += converted.samples
                        if samples > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                            raise TranscriptionError("audio_too_long")
                        output.writeframesraw(converted.to_ndarray().tobytes())
            for converted in resampler.resample(None):
                samples += converted.samples
                if samples > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                    raise TranscriptionError("audio_too_long")
                output.writeframesraw(converted.to_ndarray().tobytes())
        if samples == 0:
            raise TranscriptionError("audio_decode_failed")
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError("audio_decode_failed") from exc


def transcribe(path: Path, model_dir: Path | None = None) -> str:
    _check_input(path)
    model_dir = model_dir or Path(
        os.getenv("IG_CAPTCHA_MODEL_DIR", str(DEFAULT_MODEL_DIR))
    )
    if not model_dir.is_dir():
        raise TranscriptionError("model_unavailable")

    # This also blocks Hugging Face Hub network access if a cache is incomplete.
    os.environ["HF_HUB_OFFLINE"] = "1"
    with tempfile.TemporaryDirectory(prefix="ig-captcha-audio-") as directory:
        wav_path = Path(directory) / "clip.wav"
        _decode_bounded(path, wav_path)
        try:
            import numpy as np
            from faster_whisper import WhisperModel

            # PyAV already decoded and bounded this PCM. Passing a file path would
            # make faster-whisper decode it again through its own PyAV wrapper.
            with wave.open(str(wav_path), "rb") as pcm:
                if (
                    pcm.getnchannels() != 1
                    or pcm.getsampwidth() != 2
                    or pcm.getframerate() != SAMPLE_RATE
                    or pcm.getnframes() > MAX_AUDIO_SECONDS * SAMPLE_RATE
                ):
                    raise TranscriptionError("audio_decode_failed")
                audio = np.frombuffer(
                    pcm.readframes(MAX_AUDIO_SECONDS * SAMPLE_RATE + 1),
                    dtype=np.int16,
                ).astype(np.float32) / 32768.0
            if audio.size == 0 or audio.size > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                raise TranscriptionError("audio_decode_failed")

            model_ref = (
                str(model_dir) if (model_dir / "model.bin").is_file() else "tiny.en"
            )
            with (
                open(os.devnull, "w", encoding="utf-8") as discard,
                contextlib.redirect_stdout(discard),
            ):
                model = WhisperModel(
                    model_ref,
                    download_root=str(model_dir),
                    local_files_only=True,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=2,
                    num_workers=1,
                )
                segments, _ = model.transcribe(
                    audio,
                    language="en",
                    beam_size=1,
                )
                text = " ".join(segment.text.strip() for segment in segments).strip()
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError("transcription_failed") from exc
    if not text:
        raise TranscriptionError("empty_transcription")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_path", type=Path)
    args = parser.parse_args(argv)
    try:
        text = transcribe(args.audio_path)
    except TranscriptionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"text": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
