"""One-use login, expiry, host checks and immediate access revocation."""

import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from bot.dashboard import DashboardAccess, _hash, public_url, register_dashboard


class DashboardPublicAddressTests(unittest.TestCase):
    def test_only_live_cloudflare_status_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            with patch.dict(os.environ, {"DASHBOARD_TUNNEL_STATUS_FILE": str(path)}):
                for url in ("https://attacker.example", "http://one.trycloudflare.com",
                            "https://user@one.trycloudflare.com", "https://one.trycloudflare.com:99"):
                    path.write_text(json.dumps({"state": "ready", "expires_at": 9999999999,
                                                "url": url}))
                    self.assertIsNone(public_url())
                path.write_text(json.dumps({"state": "ready", "expires_at": 9999999999,
                                            "url": "https://one.trycloudflare.com"}))
                self.assertEqual(public_url(), "https://one.trycloudflare.com/stats/")
                path.write_text(json.dumps({"state": "ready", "expires_at": 1,
                                            "url": "https://one.trycloudflare.com"}))
                self.assertIsNone(public_url())

    def test_invalid_tokens_do_not_query_database(self):
        db = SimpleNamespace(pool=SimpleNamespace(fetchval=AsyncMock()))
        access = DashboardAccess(db, 1)

        async def check():
            self.assertIsNone(await access.consume("guess"))
            self.assertIsNone(await access.session("guess"))

        import asyncio

        asyncio.run(check())
        db.pool.fetchval.assert_not_awaited()


@unittest.skipUnless(os.environ.get("DASHBOARD_TEST_DSN"), "needs isolated PostgreSQL session")
class DashboardAccessPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.conn = await asyncpg.connect(os.environ["DASHBOARD_TEST_DSN"])
        await self.conn.execute("""
CREATE TEMP TABLE dashboard_access(token_hash text PRIMARY KEY,kind text,
 expires_at timestamptz,access_until timestamptz);
""")
        connection = self.conn

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

            async def execute(self, *args):
                return await connection.execute(*args)

            async def fetchval(self, *args):
                return await connection.fetchval(*args)

        self.db = SimpleNamespace(pool=Pool(), record_event=AsyncMock())
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DASHBOARD_LOCAL_URL": "http://127.0.0.1:30080/stats/",
            "DASHBOARD_TUNNEL_REQUEST_FILE": self.directory.name + "/request.json",
            "DASHBOARD_TUNNEL_STATUS_FILE": self.directory.name + "/status.json",
        })
        self.env.start()
        self.data = AsyncMock(return_value={"summary": {"nonstart": 42}})
        with patch("bot.dashboard_data.dashboard_data", self.data):
            app = web.Application(client_max_size=4096)
            register_dashboard(app, self.db, 1)
        self.client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.access = DashboardAccess(self.db, 1)

    async def asyncTearDown(self):
        await self.client.close()
        await self.conn.close()
        self.env.stop()
        self.directory.cleanup()

    async def login(self):
        issued = await self.access.issue(internet=False)
        response = await self.client.post("/stats/api/login", json={"ticket": issued["ticket"]})
        self.assertEqual(response.status, 200)
        return issued, response

    async def test_unauthenticated_requests_cannot_read_data(self):
        response = await self.client.get("/stats/api/data")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.data.assert_not_awaited()
        response = await self.client.get("/stats/api/data", headers={"Host": "evil.example"})
        self.assertEqual(response.status, 403)

    async def test_ticket_is_one_use_and_only_hash_is_stored(self):
        issued, response = await self.login()
        cookies = response.cookies["stats_session"]
        self.assertTrue(cookies["httponly"])
        self.assertEqual(cookies["samesite"], "Strict")
        self.assertNotEqual(await self.conn.fetchval("SELECT token_hash FROM dashboard_access"),
                            cookies.value)
        self.assertIsNone(await self.conn.fetchval(
            "SELECT token_hash FROM dashboard_access WHERE token_hash=$1", _hash(issued["ticket"]),
        ))
        response = await self.client.post("/stats/api/login", json={"ticket": issued["ticket"]})
        self.assertEqual(response.status, 401)
        response = await self.client.get("/stats/api/data?period=7d")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["summary"]["nonstart"], 42)

    async def test_logout_revokes_every_session_and_closes_window(self):
        await self.login()
        response = await self.client.post("/stats/api/logout", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM dashboard_access"), 0)
        response = await self.client.get("/stats/api/session")
        self.assertEqual(response.status, 401)
        request = json.loads(Path(self.directory.name + "/request.json").read_text())
        self.assertLessEqual(request["expires_at"], datetime.now(timezone.utc).timestamp())

    async def test_expired_ticket_and_session_are_refused(self):
        issued = await self.access.issue(internet=False)
        await self.conn.execute("UPDATE dashboard_access SET expires_at=now()-interval '1 second'")
        response = await self.client.post("/stats/api/login", json={"ticket": issued["ticket"]})
        self.assertEqual(response.status, 401)
        await self.login()
        await self.conn.execute("UPDATE dashboard_access SET expires_at=now()-interval '1 second'")
        response = await self.client.get("/stats/api/data")
        self.assertEqual(response.status, 401)
        self.data.assert_not_awaited()

    async def test_cross_origin_posts_do_not_consume_ticket_or_revoke_session(self):
        issued = await self.access.issue(internet=False)
        response = await self.client.post("/stats/api/login", json={"ticket": issued["ticket"]},
                                          headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertIsNotNone(await self.access.consume(issued["ticket"]))
        response = await self.client.post("/stats/api/logout", data="")
        self.assertEqual(response.status, 403)

    async def test_tunnel_window_and_session_share_expiry(self):
        before = datetime.now(timezone.utc)
        issued = await self.access.issue(minutes=15)
        request = json.loads(Path(self.directory.name + "/request.json").read_text())
        until = datetime.fromisoformat(issued["expires_at"])
        self.assertTrue(before + timedelta(minutes=14) < until < before + timedelta(minutes=16))
        self.assertEqual(request["expires_at"], until.timestamp())
        _, session_until = await self.access.consume(issued["ticket"])
        self.assertEqual(session_until, until)

    async def test_tunnel_cookie_stays_secure_with_uppercase_host_and_port(self):
        Path(self.directory.name + "/status.json").write_text(json.dumps({
            "state": "ready", "expires_at": 9999999999,
            "url": "https://one.trycloudflare.com",
        }))
        issued = await self.access.issue(internet=False)
        response = await self.client.post(
            "/stats/api/login", json={"ticket": issued["ticket"]},
            headers={"Host": "ONE.TRYCLOUDFLARE.COM:443"},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies["stats_session"]["secure"])

    async def test_bad_filters_and_login_flood_are_bounded(self):
        await self.login()
        response = await self.client.get("/stats/api/data?page_size=100000")
        self.assertEqual(response.status, 400)
        self.data.assert_not_awaited()
        for _ in range(10):
            response = await self.client.post("/stats/api/login", json={"ticket": "wrong"})
        self.assertEqual(response.status, 429)
