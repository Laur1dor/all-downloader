"""The VPN DNS relay uses bounded RFC 8484 wire-format GET requests."""

from __future__ import annotations

import asyncio
import base64
import struct
import unittest
from unittest.mock import patch

from bot import dns_vpn


def _query() -> bytes:
    return (
        struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        + b"\x02vt\x06tiktok\x03com\x00"
        + struct.pack("!HH", 1, 1)
    )


class _Response:
    status_code = 200

    def __init__(self, answer: bytes) -> None:
        self.content = answer


class _Session:
    def __init__(self, answer: bytes) -> None:
        self.answer = answer
        self.url: str | None = None
        self.headers: dict[str, str] | None = None

    async def get(self, url: str, *, headers: dict[str, str],
                  proxy: str, timeout: float, impersonate: str) -> _Response:
        self.url = url
        self.headers = headers
        assert proxy.startswith("socks5h://")
        assert timeout == dns_vpn.HTTP_BUDGET
        assert impersonate == "chrome"
        return _Response(self.answer)


class _SlowSession:
    async def get(self, *_args: object, **_kwargs: object) -> _Response:
        await asyncio.sleep(0.05)
        return _Response(b"")


class DnsVpnTests(unittest.TestCase):
    def test_get_preserves_wire_query_and_answer(self) -> None:
        query = _query()
        answer = query[:2] + b"\x81\x80" + query[4:]
        session = _Session(answer)
        forwarder = dns_vpn.Forwarder.__new__(dns_vpn.Forwarder)
        forwarder.session = session  # type: ignore[assignment]
        forwarder.pending = 0

        with patch.object(dns_vpn, "DOH_URL", "https://dns.google/dns-query"):
            result = asyncio.run(forwarder.resolve(query))

        self.assertEqual(result, answer)
        self.assertEqual(forwarder.pending, 0)
        self.assertEqual(session.headers, {"Accept": "application/dns-message"})
        encoded = session.url.split("?dns=", 1)[1]  # type: ignore[union-attr]
        self.assertEqual(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)),
            query,
        )

    def test_bad_answer_returns_servfail_with_query_id(self) -> None:
        query = _query()
        session = _Session(b"not a DNS answer")
        forwarder = dns_vpn.Forwarder.__new__(dns_vpn.Forwarder)
        forwarder.session = session  # type: ignore[assignment]
        forwarder.pending = 0

        result = asyncio.run(forwarder.resolve(query))

        self.assertEqual(result[:2], query[:2])
        self.assertEqual(result[2:4], b"\x81\x82")
        self.assertEqual(result[4:6], b"\x00\x01")
        self.assertEqual(result[12:], query[12:])
        self.assertEqual(forwarder.pending, 0)

    def test_servfail_retains_edns_additional_record(self) -> None:
        query = _query()
        query = query[:10] + b"\x00\x01" + query[12:]
        query += b"\x00" + struct.pack("!HHIH", 41, 1232, 0, 0)

        result = dns_vpn._servfail(query)

        self.assertEqual(result[4:12], b"\x00\x01\x00\x00\x00\x00\x00\x01")
        self.assertEqual(result[12:], query[12:])

    def test_waiting_for_curl_handle_respects_total_budget(self) -> None:
        query = _query()
        forwarder = dns_vpn.Forwarder.__new__(dns_vpn.Forwarder)
        forwarder.session = _SlowSession()  # type: ignore[assignment]
        forwarder.pending = 0

        with patch.object(dns_vpn, "HTTP_BUDGET", 0.001):
            result = asyncio.run(forwarder.resolve(query))

        self.assertEqual(result[2:4], b"\x81\x82")
        self.assertEqual(result[12:], query[12:])
        self.assertEqual(forwarder.pending, 0)


if __name__ == "__main__":
    unittest.main()
