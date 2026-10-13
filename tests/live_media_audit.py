"""Manual ffmpeg integration audit for an isolated candidate media_parts.py."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("candidate_media_parts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(path: Path) -> tuple[float, int]:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-count_frames", "-show_entries",
        "format=duration:stream=codec_name,codec_type,nb_read_frames",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    streams = data["streams"]
    assert any(s["codec_type"] == "video" and s["codec_name"] == "h264" for s in streams)
    assert all(s["codec_name"] == "aac" for s in streams if s["codec_type"] == "audio")
    frames = sum(int(s.get("nb_read_frames") or 0) for s in streams
                 if s["codec_type"] == "video")
    return float(data["format"]["duration"]), frames


def main() -> None:
    media = load_module(Path(sys.argv[1]))
    with tempfile.TemporaryDirectory(prefix="media-audit-") as temporary:
        root = Path(temporary)
        source = root / "source.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "mpeg4", "-q:v", "2", "-c:a", "mp3", str(source),
        ], timeout=90, check=True)
        trimmed = media._prepare(source, 5_000_000, (1, 3), None, True)
        assert len(trimmed) == 1
        trim_duration, trim_frames = probe(trimmed[0])
        assert abs(trim_duration - 2) < 0.3 and abs(trim_frames - 60) <= 2
        reference = root / "reference.mp4"
        shutil.copyfile(trimmed[0], reference)

        audio = media._trim_audio(source, (1, 3), None)
        audio_result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio),
        ], capture_output=True, text=True, timeout=30, check=True)
        assert abs(float(audio_result.stdout) - 2) < 0.3

        split_source = root / "split" / "source.mp4"
        split_source.parent.mkdir()
        shutil.copyfile(source, split_source)
        parts = media._prepare(split_source, 500_000, None, None)
        assert 2 <= len(parts) <= media.MAX_PARTS
        durations, frames = zip(*(probe(part) for part in parts))
        assert abs(sum(durations) - 6) < 0.5
        assert abs(sum(frames) - 180) <= 4
        assert all(part.stat().st_size <= 500_000 for part in parts)

        sparse = root / "sparse.mp4"
        with sparse.open("wb") as output:
            output.truncate(2200 * 1024**2)
        assert sparse.stat().st_size > 2 * 1024**3
        assert sparse.stat().st_blocks * 512 < 1024**2

        def fake_segment(_source, directory, _seconds, _timeout, _cancel):
            output = [directory / f"part-{i:02d}.mp4" for i in (1, 2)]
            for part in output:
                shutil.copyfile(reference, part)
            return output

        with patch.object(media, "_duration", return_value=600), \
             patch.object(media, "_source_compatible", return_value=True), \
             patch.object(media, "_segment_copy", side_effect=fake_segment):
            large_parts = media._prepare(sparse, 2000 * 1024**2, None, None)
        assert len(large_parts) == 2
        with sparse.open("wb") as output:
            output.truncate(6 * 1024**3 + 1)
        try:
            media._prepare(sparse, 2000 * 1024**2, None, None)
        except media.MediaPreparationError:
            pass
        else:
            raise AssertionError("source over 6 GiB was accepted")

        cancelled = threading.Event()
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
        began = time.monotonic()
        try:
            media._run_ffmpeg([
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=120",
                "-c:v", "libx264", "-preset", "slow", str(root / "cancel.mp4"),
            ], 30, cancelled)
        except media.MediaPreparationError:
            assert cancelled.is_set()
        else:
            raise AssertionError("ffmpeg cancellation was ignored")
        finally:
            timer.cancel()
        assert time.monotonic() - began < 5
        print(json.dumps({
            "trim_seconds": trim_duration, "audio_seconds": float(audio_result.stdout),
            "split_parts": len(parts), "split_seconds": round(sum(durations), 3),
            "split_frames": sum(frames), "sparse_gib": round(sparse.stat().st_size / 1024**3),
            "cancel_seconds": round(time.monotonic() - began, 2),
        }))
    assert not root.exists(), "temporary files were not cleaned up"


if __name__ == "__main__":
    main()
