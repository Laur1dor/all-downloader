"""The optional Xray balancer fallback must never be implicit."""

import unittest
from unittest.mock import patch

from proxy import build_config as bc

LINK = "vless://00000000-0000-0000-0000-000000000000@example.com:443?encryption=none&security=tls&type=ws"


def _config(enabled: bool) -> dict:
    with (
        patch.object(bc, "WARP_FALLBACK", enabled),
        patch.object(bc, "collect_tiktok_links", return_value=[]),
        patch.object(bc, "_pinned_services", return_value={}),
    ):
        return bc.build_config([LINK], [LINK], [LINK])



class XrayWarpFallbackTests(unittest.TestCase):

    def test_warp_fallback_is_disabled_by_default(self) -> None:
        config = _config(False)
        assert all(outbound["tag"] != "warp-fallback" for outbound in config["outbounds"])
        assert {balancer["tag"] for balancer in config["routing"]["balancers"]} == {
            "proxy", "goida", "bypass",
        }
        assert all(
            "fallbackTag" not in balancer
            for balancer in config["routing"]["balancers"]
        )


    def test_warp_fallback_applies_to_all_three_balancers(self) -> None:
        config = _config(True)
        outbound = next(
            outbound for outbound in config["outbounds"]
            if outbound["tag"] == "warp-fallback"
        )
        assert outbound["protocol"] == "socks"
        assert outbound["settings"]["servers"] == [{"address": "warp", "port": 1080}]
        fallback_tags = {
            balancer["tag"]: balancer["fallbackTag"]
            for balancer in config["routing"]["balancers"]
        }
        assert fallback_tags == {
            "proxy": "warp-fallback",
            "goida": "warp-fallback",
            "bypass": "warp-fallback",
        }
