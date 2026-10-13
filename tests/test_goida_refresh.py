"""The live pool must reload Xray when nodes rotate at a fixed size."""

from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from proxy import build_config, probe_goida


class GoidaRefreshTests(unittest.TestCase):
    def test_fresh_verified_pool_respects_configured_cap(self) -> None:
        links = [f"vless://node-{index}@example.org:443" for index in range(5)]
        with TemporaryDirectory() as directory:
            live_path = Path(directory) / "goida_live.txt"
            live_path.write_text("\n".join(links), encoding="utf-8")
            with (
                patch.object(build_config, "GOIDA_LIVE_FILE", str(live_path)),
                patch.object(build_config, "GOIDA_POOL_SIZE", 3),
                patch("sys.stderr", new_callable=StringIO),
            ):
                self.assertEqual(build_config.collect_goida_links(), links[:3])

    def test_changed_node_with_same_count_requests_rebuild(self) -> None:
        old = "vless://old@example.org:443#old"
        new = "vless://new@example.net:443#new"
        with (
            patch.object(probe_goida, "_read_live", return_value=[old]),
            patch.object(probe_goida, "_read_recent", return_value=[]),
            patch.object(probe_goida, "_candidates", return_value=[new]),
            patch.object(probe_goida, "_tcp_alive", side_effect=lambda link: link),
            patch.object(
                probe_goida, "_really_works",
                side_effect=lambda pair: pair[1] if pair[1] == new else None,
            ),
            patch.object(probe_goida, "_write_live"),
            patch.object(probe_goida, "_remember_recent"),
            patch.object(probe_goida, "_request_rebuild") as rebuild,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(probe_goida.probe_round(), [new])
        rebuild.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
