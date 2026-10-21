"""Telegram's private pin must survive independent public-pool rotation."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from proxy import build_config as bc

MAIN = "vless://11111111-1111-4111-8111-111111111111@main.example:443?security=tls"
PIN = "vless://22222222-2222-4222-8222-222222222222@free.example:443?security=tls&type=ws"
OTHER = "vless://33333333-3333-4333-8333-333333333333@rotated.example:443?security=tls"


class TelegramProxyPinTests(unittest.TestCase):
    def test_pool_rotation_preserves_pin_and_first_direct_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "telegram_node.txt"
            source.write_text(PIN + "\n", encoding="utf8")
            configs = []
            with patch.object(bc, "TELEGRAM_NODE_FILE", str(source)), \
                 patch.object(bc, "WARP_FALLBACK", True), \
                 patch.object(bc, "_pinned_services", return_value={"tiktok": MAIN}):
                for pool in ([MAIN, OTHER], [OTHER], []):
                    with patch.object(bc, "collect_tiktok_links", return_value=pool):
                        configs.append(bc.build_config([MAIN], pool, [OTHER]))
            snapshots = []
            for config in configs:
                outbound = next(o for o in config["outbounds"] if o["tag"] == "telegram-pinned")
                inbound = next(i for i in config["inbounds"] if i["tag"] == "telegram-socks")
                rule = config["routing"]["rules"][0]
                self.assertEqual(inbound["port"], 2069)
                self.assertEqual(rule, {"type": "field", "inboundTag": ["telegram-socks"],
                                        "outboundTag": "telegram-pinned"})
                self.assertNotIn("fallbackTag", outbound)
                self.assertNotIn("balancerTag", rule)
                self.assertEqual(sum(i["port"] == 2069 for i in config["inbounds"]), 1)
                snapshots.append((outbound, inbound, rule))
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[0], snapshots[2])

    def test_absent_file_does_not_add_routes_or_fallback_inbound(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bc, "TELEGRAM_NODE_FILE", str(Path(directory) / "absent")), \
             patch.object(bc, "collect_tiktok_links", return_value=[]), \
             patch.object(bc, "_pinned_services", return_value={}), \
             redirect_stderr(io.StringIO()) as errors:
            config = bc.build_config([MAIN])
        self.assertEqual({i["tag"] for i in config["inbounds"]}, {"socks", "http"})
        self.assertFalse(any(o["tag"] == "telegram-pinned" for o in config["outbounds"]))
        self.assertFalse(any("telegram-socks" in r.get("inboundTag", [])
                             for r in config["routing"]["rules"]))
        self.assertEqual(errors.getvalue(), "")

    def test_invalid_private_input_is_disabled_without_credential_leak(self) -> None:
        invalid = ("", "TOPSECRET://credentials", "vless://TOPSECRET@host.example:443",
                   "vless://TOPSECRET", PIN + "\n" + OTHER, "TOPSECRET" * 8000)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "telegram_node.txt"
            for text in invalid:
                source.write_text(text, encoding="utf8")
                with self.subTest(kind=text[:12]), \
                     patch.object(bc, "TELEGRAM_NODE_FILE", str(source)), \
                     patch.object(bc, "collect_tiktok_links", return_value=[]), \
                     patch.object(bc, "_pinned_services", return_value={}), \
                     redirect_stderr(io.StringIO()) as errors:
                    config = bc.build_config([MAIN])
                self.assertFalse(any(i["port"] in (2068, 2069) for i in config["inbounds"]))
                self.assertFalse(any(o["tag"] == "telegram-pinned" for o in config["outbounds"]))
                self.assertEqual(errors.getvalue(),
                                 "WARN: invalid private Telegram node; pin disabled\n")
                self.assertNotIn("TOPSECRET", errors.getvalue())
                self.assertNotIn(PIN, errors.getvalue())
            with patch("builtins.open", side_effect=PermissionError("TOPSECRET")), \
                 redirect_stderr(io.StringIO()) as errors:
                self.assertIsNone(bc._telegram_pinned_outbound())
            self.assertNotIn("TOPSECRET", errors.getvalue())

    def test_youtube_split_routes_only_cdn_to_pin_before_service_overrides(self) -> None:
        with patch.object(bc, "_telegram_pinned_outbound",
                          return_value=bc.link_to_outbound(PIN, "telegram-pinned")), \
             patch.object(bc, "WARP_FALLBACK", True), \
             patch.object(bc, "collect_tiktok_links", return_value=[OTHER] * 8), \
             patch.object(bc, "_pinned_services", return_value={"youtube": MAIN}):
            config = bc.build_config([MAIN])
        inbound = next(i for i in config["inbounds"] if i["tag"] == "youtube-split-socks")
        self.assertEqual(inbound["port"], 2068)
        ports = [i["port"] for i in config["inbounds"]]
        self.assertEqual(len(ports), len(set(ports)))

        def route(inbound_tag: str, host: str) -> str | None:
            for rule in config["routing"]["rules"]:
                if "inboundTag" in rule and inbound_tag not in rule["inboundTag"]:
                    continue
                if "domain" in rule and not any(
                    host == domain.removeprefix("domain:")
                    or host.endswith("." + domain.removeprefix("domain:"))
                    for domain in rule["domain"]
                ):
                    continue
                return rule.get("outboundTag")
            return None

        for host in ("googlevideo.com", "rr1.example.googlevideo.com"):
            self.assertEqual(route("youtube-split-socks", host), "telegram-pinned")
        for host in ("www.youtube.com", "youtubei.googleapis.com", "i.ytimg.com",
                     "googlevideo.com.attacker.example", "notgooglevideo.com"):
            self.assertEqual(route("youtube-split-socks", host), "warp-fallback")
        self.assertEqual(route("telegram-socks", "www.youtube.com"), "telegram-pinned")
        self.assertIsNone(route("socks", "rr1.example.googlevideo.com"))

    def test_youtube_split_warp_route_exists_without_balancer_fallback_enabled(self) -> None:
        with patch.object(bc, "_telegram_pinned_outbound",
                          return_value=bc.link_to_outbound(PIN, "telegram-pinned")), \
             patch.object(bc, "WARP_FALLBACK", False), \
             patch.object(bc, "collect_tiktok_links", return_value=[]), \
             patch.object(bc, "_pinned_services", return_value={}):
            config = bc.build_config([MAIN])
        warp = next(o for o in config["outbounds"] if o["tag"] == "warp-fallback")
        self.assertEqual(warp["settings"]["servers"], [{"address": "warp", "port": 1080}])
        self.assertTrue(all("fallbackTag" not in b for b in config["routing"]["balancers"]))


if __name__ == "__main__":
    unittest.main()
