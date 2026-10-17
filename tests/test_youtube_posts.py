"""Community images use the photo flow and share keys across YouTube URL forms."""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import download
from bot.progress import CancelRegistry
from bot.urlcache import UrlCache
from bot.urlkey import canonical_key

POST_ID = "Ugkxy36_ahOiFqKpfodrT3XQGCj4VPD-yiwT"
POST_URL = f"https://www.youtube.com/post/{POST_ID}"


class YouTubePostKeyTests(unittest.TestCase):
    def test_tracking_mobile_and_community_forms_share_one_post_key(self):
        urls = (
            POST_URL,
            f"http://youtube.com/post/{POST_ID}?surface=shorts&si=share",
            f"https://m.youtube.com/post/{POST_ID}?data=tracking",
            f"https://www.youtube.com/channel/UCexample/community?lb={POST_ID}",
            f"https://www.youtube.com/@example/community?lb={POST_ID}&si=share",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(canonical_key(url), f"youtube:post:{POST_ID}")

    def test_different_posts_and_video_keys_stay_separate(self):
        self.assertNotEqual(canonical_key(POST_URL), canonical_key(POST_URL + "more"))
        self.assertEqual(canonical_key("https://youtu.be/dQw4w9WgXcQ"), "youtube:dQw4w9WgXcQ")
        self.assertEqual(
            canonical_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=share"),
            "youtube:dQw4w9WgXcQ",
        )

    def test_posts_have_descriptions_but_no_video_quality_menu(self):
        self.assertTrue(download._description_capable(POST_URL, "youtube"))
        self.assertFalse(download._should_offer_quality(POST_URL, "youtube"))
        self.assertTrue(download._should_offer_quality(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube",
        ))


class YouTubePostRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_post(self, suffix="", album_success=True):
        message = MagicMock(
            text=POST_URL + suffix,
            from_user=SimpleNamespace(id=1, username="tester"),
        )
        message.answer = AsyncMock(return_value=MagicMock())
        db = MagicMock(upsert_user=AsyncMock())
        settings = SimpleNamespace(admin_id=1, cookies_file=None)
        patches = {
            "expand_short_link": AsyncMock(return_value=POST_URL),
            "ensure_public_url": MagicMock(),
            "_deliver_album": AsyncMock(return_value=album_success),
            "_ask_youtube_choice": AsyncMock(),
            "_select_quality": AsyncMock(),
            "_send_cached_video": AsyncMock(return_value=False),
            "download_video": MagicMock(),
            "_safe_edit": AsyncMock(),
            "_record": AsyncMock(),
        }
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(download, name, value))
            await download._run_link(message, db, settings, UrlCache(), CancelRegistry())
        return message, patches

    async def test_post_routes_directly_to_ordered_album_delivery(self):
        _, patches = await self._run_post()
        patches["_deliver_album"].assert_awaited_once()
        self.assertEqual(patches["_deliver_album"].await_args.args[3], POST_URL)
        patches["_ask_youtube_choice"].assert_not_awaited()
        patches["_select_quality"].assert_not_awaited()
        patches["_send_cached_video"].assert_not_awaited()
        patches["download_video"].assert_not_called()

    async def test_failed_post_does_not_fall_back_to_video_extractor(self):
        _, patches = await self._run_post(album_success=False)
        patches["_deliver_album"].assert_awaited_once()
        patches["_safe_edit"].assert_awaited_once()
        self.assertIn("изображения", patches["_safe_edit"].await_args.args[1])
        self.assertEqual(patches["_record"].await_args.args[2:5], ("failed", "youtube", "album"))
        patches["download_video"].assert_not_called()
        patches["_select_quality"].assert_not_awaited()

    async def test_timing_is_rejected_before_download_or_menu(self):
        message, patches = await self._run_post(" 00:01-00:05")
        self.assertIn("без таймингов", message.answer.await_args.args[0])
        patches["_deliver_album"].assert_not_awaited()
        patches["_ask_youtube_choice"].assert_not_awaited()
        patches["download_video"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
