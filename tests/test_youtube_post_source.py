"""Community post attachments retain slide order and original image geometry."""

import json
import unittest
from unittest.mock import Mock, patch

from bot import youtube_posts as posts

POST_ID = "UgkxExampleCommunityPost1234"


def image(name):
    return {"backstageImageRenderer": {"image": {"thumbnails": [
        {"url": f"https://yt3.ggpht.com/{name}=s288-c-fcrop64=1,abcd-rw-nd-v1",
         "width": 288, "height": 288},
        {"url": f"https://yt3.ggpht.com/{name}=s1440-c-fcrop64=1,abcd-rw-nd-v1",
         "width": 1440, "height": 1440},
    ]}}}


def page(attachment, **extra):
    post = {"postId": POST_ID, "backstageAttachment": attachment, **extra}
    return ("<script>var ytInitialData = "
            + json.dumps({"contents": [{"backstagePostRenderer": post}]}) + ";</script>")


class YouTubePostSourceTests(unittest.TestCase):
    def test_carousel_preserves_order_duplicates_and_removes_web_crop(self):
        result = posts.parse_post_html(page({"postMultiImageRenderer": {"images": [
            image("second"), image("first"), image("second"),
        ]}}, contentText={"runs": [{"text": "Caption "}, {"text": "#tag"}]}), POST_ID)
        self.assertEqual(result.image_urls, (
            "https://yt3.ggpht.com/second=s0", "https://yt3.ggpht.com/first=s0",
            "https://yt3.ggpht.com/second=s0",
        ))
        self.assertEqual(result.description, "Caption #tag")

    def test_single_image_and_alternative_assignment(self):
        html = page(image("original"), contentText={"simpleText": "  text  "})
        html = html.replace("var ytInitialData =", 'window["ytInitialData"] =')
        self.assertEqual(posts.parse_post_html(html, POST_ID).image_urls,
                         ("https://yt3.ggpht.com/original=s0",))
        self.assertEqual(posts.parse_post_html(html, POST_ID).description, "text")

    def test_exact_post_only_ignores_avatar_other_post_and_video_thumbnail(self):
        other = {"backstagePostRenderer": {
            "postId": "UgkxOtherPost", "backstageAttachment": image("wrong"),
        }}
        wanted = {"backstagePostRenderer": {"postId": POST_ID, "authorThumbnail": image("avatar"),
                                         "backstageAttachment": {"videoRenderer": image("video")}}}
        html = "ytInitialData = " + json.dumps({"contents": [other, wanted]})
        with self.assertRaises(posts.YouTubePostError):
            posts.parse_post_html(html, POST_ID)

    def test_bad_slide_fails_whole_album_instead_of_returning_partial(self):
        for broken in ({}, None, image("bad")):
            if isinstance(broken, dict) and "backstageImageRenderer" in broken:
                broken["backstageImageRenderer"]["image"]["thumbnails"][0]["url"] = "https://localhost/image"
                broken["backstageImageRenderer"]["image"]["thumbnails"][1]["url"] = "https://yt3.ggpht.com.evil.example/image"
            with self.subTest(broken=broken), self.assertRaises(posts.YouTubePostError):
                posts.parse_post_html(page({
                    "postMultiImageRenderer": {"images": [image("good"), broken]},
                }), POST_ID)

    def test_direct_post_url_validation_and_tracking_removal(self):
        for host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            url = f"http://{host}/post/{POST_ID}?surface=shorts&si=tracking"
            self.assertEqual(posts.post_id(url), POST_ID)
            self.assertEqual(posts.canonical_post_url(url), f"https://www.youtube.com/post/{POST_ID}")
        for url in (f"https://youtube.com.evil.org/post/{POST_ID}", f"https://user@youtube.com/post/{POST_ID}",
                    f"https://youtube.com:123/post/{POST_ID}", f"ftp://youtube.com/post/{POST_ID}",
                    "https://youtube.com/watch?v=example", "https://youtube.com/post/invalid"):
            self.assertFalse(posts.is_post_url(url), url)

    def test_oversized_page_and_missing_data_rejected(self):
        for html in ("", "ytInitialData = broken;", "x" * (posts._PAGE_LIMIT + 1)):
            with self.subTest(length=len(html)), self.assertRaises(posts.YouTubePostError):
                posts.parse_post_html(html, POST_ID)

    def test_resolver_bounded_stream_and_no_redirects(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [page(image("photo")).encode()]
        session = Mock()
        session.get.return_value = response
        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = posts.resolve_post(f"https://youtube.com/post/{POST_ID}?si=tracking", "socks5://proxy")
        self.assertEqual(result.image_urls, ("https://yt3.ggpht.com/photo=s0",))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.get.call_args.args[0], f"https://www.youtube.com/post/{POST_ID}")
        response.close.assert_called_once()

    def test_resolver_rejects_oversized_stream(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b"x" * (posts._PAGE_LIMIT + 1)]
        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaises(posts.YouTubePostError):
                posts.resolve_post(f"https://youtube.com/post/{POST_ID}")
        response.close.assert_called_once()

    def test_network_failure_is_a_user_facing_post_error(self):
        from curl_cffi.requests.exceptions import Timeout

        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.__enter__.return_value.get.side_effect = Timeout("upstream")
            with self.assertRaises(posts.YouTubePostError):
                posts.resolve_post(f"https://youtube.com/post/{POST_ID}")
