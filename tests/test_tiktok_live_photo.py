"""Native motion belongs to its own slide; originals survive API preparation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import tiktok_live_photo as live
from bot.urlguard import BlockedAddressError


class LivePhotoMetadataTests(unittest.TestCase):
    def test_verified_tikwm_parallel_motion_preserves_last_slide_pair(self):
        data = {
            "images": ["https://cdn.example/first.jpg", "https://cdn.example/second.jpg",
                       "https://cdn.example/third.jpg"],
            "live_images": [None, None, "https://cdn.example/third-live.mp4"],
        }
        with patch.object(live, "ensure_public_url"):
            sources = live.extract_live_photo_sources(data)
        self.assertEqual([source.index for source in sources], [0, 1, 2])
        self.assertEqual([source.motion_urls for source in sources],
                         [(), (), ("https://cdn.example/third-live.mp4",)])
        self.assertEqual(sources[2].photo_urls, ("https://cdn.example/third.jpg",))

    def test_misaligned_or_invalid_parallel_motion_is_rejected(self):
        for motions in ([None], [None, None, None], "motion.mp4", [None, {}], [None, ""]):
            with (
                self.subTest(motions=motions), patch.object(live, "ensure_public_url"),
                self.assertRaises(live.LivePhotoError),
            ):
                live.extract_live_photo_sources({
                    "images": ["https://cdn.example/first.jpg", "https://cdn.example/second.jpg"],
                    "live_images": motions,
                })

    def test_mixed_slides_keep_their_own_motion_and_original_order(self):
        item = {"imagePost": {"images": [
            {"imageURL": {"urlList": ["https://cdn.example/first.jpg"]}},
            {"imageURL": {"urlList": ["https://cdn.example/second.jpg"]},
             "videoURL": {"urlList": ["https://cdn.example/second.mp4"]}},
        ]}}
        with patch.object(live, "ensure_public_url"):
            sources = live.extract_live_photo_sources(item)
        self.assertEqual([source.index for source in sources], [0, 1])
        self.assertEqual(sources[0].motion_urls, ())
        self.assertEqual(sources[1].motion_urls, ("https://cdn.example/second.mp4",))

    def test_mobile_motion_stays_paired_without_using_background_music(self):
        item = {"video": {"playAddr": "https://cdn.example/unrelated.mp4"},
                "music": {"playUrl": "https://cdn.example/music.mp3"},
                "image_post_info": {"images": [
                    {"url_list": ["https://cdn.example/photo.jpg"],
                     "video": {"play_addr": {"url_list": ["https://cdn.example/live.mp4"]}}},
                ]}}
        with patch.object(live, "ensure_public_url"):
            sources = live.extract_live_photo_sources(item)
        self.assertEqual(sources[0].motion_urls, ("https://cdn.example/live.mp4",))

    def test_missing_and_ambiguous_motion_are_errors(self):
        for fields in (
            {"isLivePhoto": True},
            {"videoURL": {"uri": "missing-url"}},
            {"videoURL": "https://cdn.example/a.mp4",
             "livePhotoURL": "https://cdn.example/b.mp4"},
        ):
            with (
                self.subTest(fields=fields), patch.object(live, "ensure_public_url"),
                self.assertRaises(live.LivePhotoError),
            ):
                live.extract_live_photo_sources({"imagePost": {"images": [
                    {"imageURL": "https://cdn.example/photo.jpg", **fields},
                ]}})

    def test_static_slide_never_uses_post_video_as_live_motion(self):
        item = {"images": ["https://cdn.example/photo.jpg"],
                "video": {"playAddr": "https://cdn.example/slideshow.mp4"}}
        with patch.object(live, "ensure_public_url"):
            self.assertEqual(live.extract_live_photo_sources(item)[0].motion_urls, ())

    def test_private_media_urls_are_rejected(self):
        with self.assertRaises(BlockedAddressError):
            live.extract_live_photo_sources({"images": ["http://127.0.0.1/photo.jpg"]})


class LivePhotoPreparationTests(unittest.TestCase):
    def test_compatible_motion_is_remuxed_and_original_photo_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo, motion = root / "source.jpg", root / "source.mov"
            photo.write_bytes(b"original-jpeg")
            motion.write_bytes(b"original-motion")
            photo_stream = {"codec_name": "mjpeg", "width": 1200, "height": 800}
            video_stream = {"codec_name": "h264", "pix_fmt": "yuv420p",
                            "width": 1200, "height": 800}

            def encode(command, _timeout, _cancel):
                Path(command[-1]).write_bytes(b"compatible-mp4")

            with (
                patch.object(live, "_probe", side_effect=[
                    (photo_stream, {}), (video_stream, {"duration": "3", "audio_codecs": ["aac"]}),
                    (video_stream, {"duration": "3"}),
                ]),
                patch.object(live, "_run_ffmpeg", side_effect=encode) as ffmpeg,
            ):
                result = live.prepare_live_photo(photo, motion, root / "prepared")
            self.assertEqual(result.photo, photo)
            self.assertEqual(result.original_photo, photo)
            self.assertEqual(result.original_motion, motion)
            self.assertEqual(photo.read_bytes(), b"original-jpeg")
            self.assertEqual(motion.read_bytes(), b"original-motion")
            self.assertIn("copy", ffmpeg.call_args.args[0])
            self.assertNotIn("-t", ffmpeg.call_args.args[0])

    def test_long_or_unknown_motion_is_not_silently_trimmed(self):
        for duration in ("10.01", "0", "nan", "unknown"):
            with self.subTest(duration=duration), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                photo, motion = root / "photo.jpg", root / "motion.mp4"
                photo.write_bytes(b"jpeg")
                motion.write_bytes(b"motion")
                with (
                    patch.object(live, "_probe", side_effect=[
                        ({"width": 100, "height": 100}, {}),
                        ({"width": 100, "height": 100}, {"duration": duration}),
                    ]),
                    patch.object(live, "_run_ffmpeg") as ffmpeg,
                    self.assertRaises(live.LivePhotoError),
                ):
                    live.prepare_live_photo(photo, motion, root / "prepared")
                ffmpeg.assert_not_called()

    def test_output_never_overwrites_original_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo, motion = root / "source.jpg", root / "live-motion.mp4"
            photo.write_bytes(b"jpeg")
            motion.write_bytes(b"source")
            with (
                patch.object(live, "_probe", side_effect=[
                    ({"codec_name": "mjpeg", "width": 100, "height": 100}, {}),
                    ({"width": 100, "height": 100}, {"duration": "3"}),
                ]),
                patch.object(live, "_run_ffmpeg") as ffmpeg,
                self.assertRaises(live.LivePhotoError),
            ):
                live.prepare_live_photo(photo, motion, root)
            ffmpeg.assert_not_called()
            self.assertEqual(motion.read_bytes(), b"source")


class EmbeddedMotionTests(unittest.TestCase):
    @staticmethod
    def packet(attributes, body=""):
        return (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:Camera="http://ns.google.com/photos/1.0/camera/" '
            'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
            'xmlns:Item="http://ns.google.com/photos/1.0/container/item/">'
            f'<description {attributes}>{body}</description></x:xmpmeta>'
        ).encode()

    def test_container_and_legacy_offsets_extract_only_declared_motion(self):
        video = (24).to_bytes(4, "big") + b"ftypisom" + b"\0" * 12 + b"video-track"
        directory = (
            '<Container:Directory><Container:Item Item:Mime="image/jpeg" '
            'Item:Semantic="Primary"/><Container:Item Item:Mime="video/mp4" '
            f'Item:Semantic="MotionPhoto" Item:Length="{len(video)}"/>'
            '</Container:Directory>'
        )
        for metadata in (
            self.packet('Camera:MotionPhoto="1"', directory),
            self.packet(f'Camera:MicroVideo="1" Camera:MicroVideoOffset="{len(video)}"'),
        ):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder) / "source.jpg", Path(folder) / "motion.mp4"
                original = b"\xff\xd8" + metadata + b"jpeg-data\xff\xd9" + video
                source.write_bytes(original)
                with patch.object(live, "_probe", return_value=(
                    {"width": 100, "height": 100}, {"duration": "3"},
                )):
                    self.assertEqual(live.extract_embedded_motion(source, target), target)
                self.assertEqual(target.read_bytes(), video)
                self.assertEqual(source.read_bytes(), original)

    def test_static_or_disabled_motion_is_not_inferred_from_video_signature(self):
        for metadata in (b"", self.packet('Camera:MotionPhoto="0"')):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder) / "source.jpg", Path(folder) / "motion.mp4"
                source.write_bytes(b"\xff\xd8" + metadata + b"\xff\xd9\0\0\0\x18ftypisom")
                with patch.object(live, "_probe") as probe:
                    self.assertIsNone(live.extract_embedded_motion(source, target))
                probe.assert_not_called()
                self.assertFalse(target.exists())

    def test_broken_declared_motion_is_an_error(self):
        for attributes in (
            'Camera:MotionPhoto="1"',
            'Camera:MicroVideo="1" Camera:MicroVideoOffset="999999"',
            'Camera:MicroVideo="1" Camera:MicroVideoOffset="16"',
        ):
            with self.subTest(attributes=attributes), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder) / "source.jpg", Path(folder) / "motion.mp4"
                source.write_bytes(b"\xff\xd8" + self.packet(attributes) + b"jpeg-data\xff\xd9")
                with self.assertRaises(live.LivePhotoError):
                    live.extract_embedded_motion(source, target)
                self.assertFalse(target.exists())

    def test_existing_destination_is_preserved(self):
        video = (24).to_bytes(4, "big") + b"ftypisom" + b"\0" * 12
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source.jpg", Path(folder) / "motion.mp4"
            source.write_bytes(b"\xff\xd8" + self.packet(
                'Camera:MicroVideo="1" Camera:MicroVideoOffset="24"',
            ) + b"\xff\xd9" + video)
            target.write_bytes(b"existing-motion")
            with self.assertRaises(FileExistsError):
                live.extract_embedded_motion(source, target)
            self.assertEqual(target.read_bytes(), b"existing-motion")
