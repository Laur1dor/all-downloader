"""Real GPU regression; the private HDR fixture is never shipped in Git."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from bot.downloader import _TIKTOK_NVENC_ENCODER_ARGS


@unittest.skipUnless(os.getenv("NVENC_QUALITY_FIXTURE"), "requires NVIDIA and private HDR fixture")
class NvidiaQualityTests(unittest.TestCase):
    def test_real_hdr_motion_has_no_quality_or_size_regression(self):
        source = Path(os.environ["NVENC_QUALITY_FIXTURE"])
        before = hashlib.sha256(source.read_bytes()).hexdigest()

        def run(command):
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            return result.stdout

        probe = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,color_transfer", "-of", "json", str(source)]))
        video = probe["streams"][0]
        self.assertIn(video["color_transfer"], ("arib-std-b67", "smpte2084"))
        width, height = video["width"], video["height"]
        # Frozen CPU colour contract, also covered by the byte-identical
        # source-document suite. This scene reproduced the bad B-frame dips.
        colour = ("zscale=t=linear:p=bt709:npl=100,format=gbrpf32le,"
                  "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
                  "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2',format=yuv420p")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.yuv"
            run(["ffmpeg", "-v", "error", "-xerror", "-y", "-threads", "1",
                 "-filter_threads", "1", "-ss", "3", "-i", str(source),
                 "-map", "0:v:0", "-frames:v", "60", "-an", "-vf", colour,
                 "-f", "rawvideo", str(reference)])
            metrics = {}
            sizes = {}
            for name in ("cpu", "nvenc"):
                output = root / f"{name}.mp4"
                command = ["ffmpeg", "-v", "error", "-y", "-threads", "1",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
                    "-r", "60", "-i", str(reference), "-an"]
                if name == "cpu":
                    command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-threads", "1"]
                else:
                    command += list(_TIKTOK_NVENC_ENCODER_ARGS)
                command += ["-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+faststart", str(output)]
                run(command)
                sizes[name] = output.stat().st_size
                graph = (f"[0:v]setpts=PTS-STARTPTS,split[a][b];"
                         f"[1:v]setpts=PTS-STARTPTS,split[c][d];"
                         f"[a][c]ssim=stats_file={root}/{name}.ssim[s];"
                         f"[b][d]psnr=stats_file={root}/{name}.psnr[p]")
                run(["ffmpeg", "-v", "error", "-threads", "1", "-i", str(output),
                     "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
                     "-r", "60", "-i", str(reference), "-filter_complex_threads", "1",
                     "-filter_complex", graph, "-map", "[s]", "-map", "[p]", "-f", "null", "-"])
                for metric, key in (("ssim", "All"), ("psnr", "psnr_avg")):
                    values = [float(re.search(r"\b" + key + r":([^ ]+)", line)[1])
                              for line in (root / f"{name}.{metric}").read_text().splitlines()]
                    self.assertEqual(len(values), 60)
                    values = [100.0 if math.isinf(value) else value for value in values]
                    metrics[name, metric] = (sum(values) / len(values), min(values))
            self.assertLessEqual(sizes["nvenc"], sizes["cpu"], sizes)
            for metric in ("ssim", "psnr"):
                for index in (0, 1):
                    self.assertGreaterEqual(metrics["nvenc", metric][index],
                                            metrics["cpu", metric][index], metrics)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
