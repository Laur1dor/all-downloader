"""Bounded DNS-over-HTTPS fallback through the existing WARP SOCKS proxy.

dnsproxy sends queries here only when its direct encrypted upstreams fail.
The DoH connection itself uses SOCKS5 with remote name resolution, so a broken
host/router resolver is not needed to reach the fallback.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

LOG = logging.getLogger(__name__)
MAX_MESSAGE = 4096
MAX_PENDING = 64
LISTEN_PORT = 5353
HTTP_BUDGET = 4.5
DOH_URL = os.getenv("DNS_VPN_DOH_URL", "https://94.140.14.14/dns-query")
SOCKS_URL = os.getenv("DNS_VPN_SOCKS_URL", "socks5://warp:1080")


def _doh_get_url(query: bytes) -> str:
    """RFC 8484 GET keeps the DNS wire message intact across the SOCKS route."""
    encoded = base64.urlsafe_b64encode(query).rstrip(b"=").decode("ascii")
    separator = "&" if "?" in DOH_URL else "?"
    return f"{DOH_URL}{separator}dns={encoded}"


def _servfail(query: bytes) -> bytes:
    if len(query) < 2:
        return b""
    if len(query) >= 12:
        _, _, questions, answers, authorities, additional = struct.unpack(
            "!HHHHHH", query[:12]
        )
        if questions == 1 and answers == 0 and authorities == 0:
            # Keep the question and optional EDNS record. dnsproxy rejects a
            # response that announces zero questions for a valid query.
            return (
                struct.pack("!HHHHHH", int.from_bytes(query[:2], "big"),
                            0x8182, 1, 0, 0, additional)
                + query[12:]
            )
    return struct.pack("!HHHHHH", int.from_bytes(query[:2], "big"), 0x8182, 0, 0, 0, 0)


class Forwarder:
    def __init__(self) -> None:
        self.session = AsyncSession(
            max_clients=8,
            curl_options={CurlOpt.MAXFILESIZE_LARGE: MAX_MESSAGE},
        )
        self.pending = 0

    async def close(self) -> None:
        await self.session.close()

    async def resolve(self, query: bytes) -> bytes:
        if not 12 <= len(query) <= MAX_MESSAGE:
            return _servfail(query)
        if self.pending >= MAX_PENDING:
            return _servfail(query)
        self.pending += 1
        try:
            # This deadline includes waiting for one of the eight curl handles.
            async with asyncio.timeout(HTTP_BUDGET):
                response = await self.session.get(
                    _doh_get_url(query),
                    headers={"Accept": "application/dns-message"},
                    proxy=SOCKS_URL.replace("socks5://", "socks5h://", 1),
                    timeout=HTTP_BUDGET,
                    impersonate="chrome",
                )
                answer = response.content
                if (response.status_code != 200 or not 12 <= len(answer) <= MAX_MESSAGE
                        or answer[:2] != query[:2] or not answer[2] & 0x80):
                    return _servfail(query)
                return answer
        except (RequestException, asyncio.TimeoutError, OSError) as exc:
            LOG.warning("DNS VPN fallback unavailable: %s", type(exc).__name__)
            return _servfail(query)
        finally:
            self.pending -= 1


class UDPServer(asyncio.DatagramProtocol):
    def __init__(self, forwarder: Forwarder) -> None:
        self.forwarder = forwarder
        self.transport: asyncio.DatagramTransport | None = None
        self.inflight = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.inflight >= MAX_PENDING:
            answer = _servfail(data)
            if answer and self.transport:
                self.transport.sendto(answer, addr)
            return
        self.inflight += 1
        asyncio.create_task(self._answer(data, addr))

    async def _answer(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            answer = await self.forwarder.resolve(data)
            if answer and self.transport:
                self.transport.sendto(answer, addr)
        finally:
            self.inflight -= 1


async def _tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      forwarder: Forwarder) -> None:
    try:
        while True:
            size = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 10))[0]
            if not 12 <= size <= MAX_MESSAGE:
                break
            query = await asyncio.wait_for(reader.readexactly(size), 10)
            answer = await forwarder.resolve(query)
            writer.write(struct.pack("!H", len(answer)) + answer)
            await asyncio.wait_for(writer.drain(), 10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    forwarder = Forwarder()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPServer(forwarder), local_addr=("0.0.0.0", LISTEN_PORT)
    )
    server = await asyncio.start_server(
        lambda r, w: _tcp_client(r, w, forwarder), "0.0.0.0", LISTEN_PORT
    )
    try:
        async with server:
            await server.serve_forever()
    finally:
        transport.close()
        await forwarder.close()


if __name__ == "__main__":
    asyncio.run(main())
