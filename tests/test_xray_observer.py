"""Every balancer must see health data from the same Xray observer."""

import unittest
from unittest.mock import patch

from proxy import build_config as bc

LINK = (
    "vless://00000000-0000-0000-0000-000000000000@example.com:443"
    "?encryption=none&security=tls&type=ws"
)


class XrayObserverTests(unittest.TestCase):
    def test_goida_uses_one_observer_for_all_balancers(self) -> None:
        with (
            patch.object(bc, "collect_tiktok_links", return_value=[]),
            patch.object(bc, "_pinned_services", return_value={}),
        ):
            config = bc.build_config([LINK], [LINK], [LINK])

        self.assertNotIn("observatory", config)
        self.assertEqual(
            set(config["burstObservatory"]["subjectSelector"]),
            {"gvless-", "vless-", "byp-"},
        )

    def test_without_goida_retains_plain_observer(self) -> None:
        with (
            patch.object(bc, "collect_tiktok_links", return_value=[]),
            patch.object(bc, "_pinned_services", return_value={}),
        ):
            config = bc.build_config([LINK], [], [LINK])

        self.assertIn("observatory", config)
        self.assertNotIn("burstObservatory", config)
        self.assertEqual(
            config["observatory"]["subjectSelector"],
            ["vless-", "byp-"],
        )


if __name__ == "__main__":
    unittest.main()
