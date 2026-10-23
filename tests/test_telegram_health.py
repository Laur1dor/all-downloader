"""Exercise stalled/lying SOCKS peers with real TCP, plus failover hysteresis."""

from __future__ import annotations

import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from proxy import telegram_health as th


class ProtocolTests(unittest.TestCase):
    def exchange(self, behavior: str, obfuscated: bool = False) -> tuple[bool, float]:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            def peer():
                with server.accept()[0] as sock:
                    sock.settimeout(1)
                    try:
                        sock.recv(3)
                        sock.sendall(b"\x05\x00")
                        sock.recv(10)
                        sock.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
                        encrypt = None
                        if obfuscated:
                            header = th._exact(sock, 64, time.monotonic() + 1)
                            decrypt = th._CTR(header[8:40], header[40:56])
                            decoded = decrypt.update(header)
                            self.assertEqual(decoded[56:60], b'\xef' * 4)
                            reverse = header[8:56][::-1]
                            encrypt = th._CTR(reverse[:32], reverse[32:])
                        request = bytearray()
                        length = 41 if obfuscated else 42
                        while len(request) < length:
                            chunk = sock.recv(length - len(request))
                            if not chunk:
                                return
                            request.extend(chunk)
                        if obfuscated:
                            request = decrypt.update(bytes(request))
                        if behavior == "plain_only" and obfuscated:
                            time.sleep(0.2)
                            return
                        if behavior == "stall":
                            time.sleep(0.2)
                            return
                        if behavior == "close":
                            return
                        if behavior == "oversize":
                            sock.sendall(b"\x7f\xff\xff\xff")
                            return
                        nonce = bytes(request[-16:])
                        body = struct.pack("<I", 0x05162463) + nonce + b"\0" * 60
                        response = b"\0" * 8 + struct.pack("<QI", 1, len(body)) + body
                        if behavior == "wrong_nonce":
                            response = response[:24] + b"x" * 16 + response[40:]
                        response = bytes([len(response) // 4]) + response
                        if encrypt:
                            response = encrypt.update(response)
                        for chunk in (response[:7], response[7:]):
                            sock.sendall(chunk)
                    except (OSError, TimeoutError):
                        pass
            worker = threading.Thread(target=peer)
            worker.start()
            began = time.monotonic()
            result = th.probe(server.getsockname(), "149.154.167.51", timeout=0.12,
                              obfuscated=obfuscated)
            elapsed = time.monotonic() - began
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            return result, elapsed

    def test_real_fragmented_mtproto_response(self):
        self.assertTrue(self.exchange("good")[0])

    def test_obfuscated_response_and_plain_only_false_positive(self):
        self.assertTrue(self.exchange("good", True)[0])
        self.assertTrue(self.exchange("plain_only")[0])
        self.assertFalse(self.exchange("plain_only", True)[0])

    def test_openssl_ctr_matches_nist_vector(self):
        cipher = th._CTR(bytes.fromhex('603deb1015ca71be2b73aef0857d7781'
                                      '1f352c073b6108d72d9810a30914dff4'),
                         bytes.fromhex('f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff'))
        self.assertEqual(cipher.update(bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')).hex(),
                         '601ec313775789a5b7a7f504bbf3d228')

    def test_socks_acceptance_with_stalled_telegram_is_not_healthy(self):
        result, elapsed = self.exchange("stall")
        self.assertFalse(result)
        self.assertLess(elapsed, 0.6)

    def test_closed_peer_wrong_nonce_and_unbounded_reply_rejected(self):
        for behavior in ("close", "wrong_nonce", "oversize"):
            with self.subTest(behavior=behavior):
                self.assertFalse(self.exchange(behavior)[0])


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.pin = th.bc.link_to_outbound(
            "vless://22222222-2222-4222-8222-222222222222@pin.example:443?security=tls", "telegram-pinned")
        self.source = root / "telegram_node.txt"
        self.source.write_text("original-private-pin")
        self.state = root / "state.json"
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"outbounds": [self.pin]}))
        self.rebuild = root / "rebuild"
        for name, value in (("STATE", self.state), ("CONFIG", self.config), ("REBUILD", self.rebuild)):
            item = patch.object(th, name, value)
            item.start(); self.addCleanup(item.stop)
        item = patch.object(th.bc, "TELEGRAM_NODE_FILE", str(self.source))
        item.start(); self.addCleanup(item.stop)
        item = patch.object(th.bc, "_telegram_pinned_outbound", return_value=self.pin)
        self.read_pin = item.start(); self.addCleanup(item.stop)

    def test_healthy_pin_and_single_blip_never_rotate(self):
        with patch.object(th, "healthy", side_effect=[False, True, False]), \
             patch.object(th, "choose_candidate") as choose:
            self.assertEqual([th.check_once() for _ in range(3)],
                             ["unhealthy", "healthy", "unhealthy"])
        choose.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["failures"], 1)
        self.assertFalse(self.rebuild.exists())

    def test_three_failures_switch_to_verified_backup_and_request_reload(self):
        with patch.object(th, "healthy", return_value=False), \
             patch.object(th, "choose_candidate", return_value="verified-free-backup") as choose:
            self.assertEqual([th.check_once() for _ in range(3)],
                             ["unhealthy", "unhealthy", "switched"])
        choose.assert_called_once_with(self.pin)
        self.assertEqual(self.source.read_text(), "verified-free-backup\n")
        self.assertTrue(self.rebuild.exists())

    def test_no_working_backup_never_overwrites_pin(self):
        with patch.object(th, "healthy", return_value=False), \
             patch.object(th, "choose_candidate", return_value=None):
            for _ in range(3):
                th.check_once()
        self.assertEqual(self.source.read_text(), "original-private-pin")
        self.assertFalse(self.rebuild.exists())

    def test_pending_config_and_disabled_pin_are_not_probed(self):
        self.config.write_text(json.dumps({"outbounds": []}))
        with patch.object(th, "healthy") as healthy:
            self.assertEqual(th.check_once(), "pending")
            self.read_pin.return_value = None
            self.assertEqual(th.check_once(), "disabled")
        healthy.assert_not_called()

    def test_operator_change_during_probe_wins(self):
        with patch.object(th, "healthy", return_value=False), \
             patch.object(th, "choose_candidate", return_value="backup"):
            th.check_once(); th.check_once()
            self.read_pin.side_effect = [self.pin, {"tag": "operator-change"}]
            self.assertEqual(th.check_once(), "unhealthy")
        self.assertFalse(self.rebuild.exists())


if __name__ == "__main__":
    unittest.main()
