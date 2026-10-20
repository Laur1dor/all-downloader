"""Dashboard validation and optional PostgreSQL-backed filter regression tests."""

import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from bot.dashboard_data import dashboard_data, parse_filters

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class DashboardFilterTests(unittest.TestCase):
    def test_periods_and_utc_custom_dates(self):
        self.assertEqual(parse_filters({}, now=NOW)["start"], "2026-09-20T12:00:00+00:00")
        self.assertIsNone(parse_filters({"period": "all"}, now=NOW)["start"])
        custom = parse_filters({"period": "custom", "start": "2026-10-01", "end": "2026-10-20"})
        self.assertEqual(custom["end"], "2026-10-21T00:00:00+00:00")
        self.assertEqual(parse_filters(custom)["end"], custom["end"])
        self.assertTrue(parse_filters({})["exclude_owner"])
        self.assertFalse(parse_filters({"exclude_owner": "false"})["exclude_owner"])

    def test_reject_invalid_filters_and_unbounded_pages(self):
        invalid = (
            {"period": "forever"},
            {"period": "custom"},
            {"period": "custom", "start": "bad", "end": "2026-10-20"},
            {"period": "custom", "start": "2026-10-21", "end": "2026-10-20"},
            {"page_size": "101"},
            {"page": "0"},
            {"page": "no"},
            {"exclude_owner": "maybe"},
            {"q": "x" * 101},
            {"q": "a\nb"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_filters(value)

    def test_sql_metacharacters_remain_literal_filter_values(self):
        value = "%' OR 1=1; --"
        self.assertEqual(parse_filters({"q": value})["q"], value)


@unittest.skipUnless(os.environ.get("DASHBOARD_TEST_DSN"), "needs isolated PostgreSQL session")
class DashboardPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.conn = await asyncpg.connect(os.environ["DASHBOARD_TEST_DSN"])
        await self.conn.execute("""
CREATE TEMP TABLE users (telegram_id bigint, username text, registration_date timestamptz);
CREATE TEMP TABLE conversions (id bigint, telegram_id bigint, created_at timestamptz,
 status text, source_platform text, media_type text, file_size bigint,
 processing_time double precision);
CREATE TEMP TABLE user_events (id bigint, telegram_id bigint, created_at timestamptz,
 action text, outcome text, duration_ms int);
CREATE TEMP TABLE upload_samples (created_at timestamptz, platform text, is_admin bool,
 size_bytes bigint, seconds double precision, waited_seconds double precision);
INSERT INTO users SELECT n, 'user' || n, '2026-01-01'::timestamptz FROM generate_series(1,5) n;
INSERT INTO conversions VALUES
 (1,1,'2026-10-19','done','tiktok','video',999,1),
 (2,3,'2026-10-19','done','youtube','photo',100,2),
 (3,3,'2026-10-19','failed','tiktok','video',NULL,3),
 (4,3,'2026-09-01','cancelled','tiktok','video',NULL,4);
INSERT INTO user_events VALUES
 (1,2,'2026-10-19','command:start','ok',10),
 (2,4,'2026-10-19','callback:cancel','cancelled',20),
 (3,1,'2026-10-19','system:startup','ok',NULL);
INSERT INTO upload_samples VALUES ('2026-10-19','youtube',false,100,2,1);
""")
        conn = self.conn

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        self.db = type("DB", (), {"pool": Pool()})()

    async def asyncTearDown(self):
        await self.conn.close()

    async def snapshot(self, **filters):
        return await dashboard_data(
            self.db, {"period": "custom", "start": "2026-10-01", "end": "2026-10-20", **filters}, 1
        )

    async def test_population_counts_start_only_and_service_history(self):
        result = await self.snapshot()
        summary = result["summary"]
        self.assertEqual(
            (summary["registered"], summary["nonstart"], summary["converters"], summary["active"]),
            (4, 2, 1, 3),
        )
        self.assertEqual(
            (summary["conversions"], summary["done"], summary["failed"], summary["bytes"]),
            (2, 1, 1, 100),
        )
        self.assertEqual(summary["service_events"], 1)
        self.assertEqual(result["pagination"]["users_total"], 4)
        self.assertTrue(any(row["kind"] == "service" for row in result["activity"]))
        self.assertEqual(result["history"]["conversions_since"][:10], "2026-09-01")
        self.assertEqual(result["uploads"]["bytes"], 100)
        self.assertEqual(result["series"][0]["service_events"], 1)
        self.assertEqual((await self.snapshot(exclude_owner=False))["summary"]["nonstart"], 3)

    async def test_jit_is_disabled_only_inside_dashboard_transaction(self):
        await self.conn.execute("SET jit = on")
        conn = self.conn
        testcase = self
        checks = []

        class CheckedConnection:
            def __getattr__(self, name):
                return getattr(conn, name)

            async def fetchrow(self, *args, **kwargs):
                checks.append(await conn.fetchval("SELECT current_setting('jit')"))
                testcase.assertEqual(checks[-1], "off")
                return await conn.fetchrow(*args, **kwargs)

        class CheckedPool:
            @asynccontextmanager
            async def acquire(self):
                yield CheckedConnection()

        self.db.pool = CheckedPool()
        await self.snapshot()
        self.assertTrue(checks)
        self.assertEqual(await conn.fetchval("SELECT current_setting('jit')"), "on")

    async def test_legacy_actors_without_registration_are_not_lost(self):
        await self.conn.execute("""
INSERT INTO conversions VALUES (5,6,'2026-10-19','done','youtube','photo',200,1);
INSERT INTO user_events VALUES
 (4,7,'2026-10-19','message:text','ok',10),
 (5,8,'2026-10-19','system:startup','ok',NULL);
""")
        result = await self.snapshot()
        summary = result["summary"]
        self.assertEqual(summary["registered"], 4)
        self.assertEqual(summary["nonstart"], 4)
        self.assertEqual(summary["converters"], 2)
        self.assertEqual(summary["active"], 5)
        self.assertEqual(result["pagination"]["users_total"], 6)
        orphan = next(row for row in result["users"] if row["telegram_id"] == 6)
        self.assertIsNone(orphan["username"])
        self.assertIsNone(orphan["registration_date"])
        self.assertEqual(orphan["conversions"], 1)
        self.assertFalse(any(row["telegram_id"] == 8 for row in result["users"]))
        self.assertEqual(summary["service_events"], 2)
        by_id = await self.snapshot(q="6")
        self.assertEqual(by_id["summary"]["registered"], 0)
        self.assertEqual(by_id["summary"]["converters"], 1)
        self.assertEqual(by_id["users"][0]["telegram_id"], 6)

    async def test_filters_are_literal_and_do_not_mix_unattributed_events(self):
        result = await self.snapshot(platform="youtube", media_type="photo")
        self.assertEqual(result["summary"]["conversions"], 1)
        self.assertEqual(result["summary"]["events"], 0)
        self.assertEqual(result["summary"]["service_events"], 0)
        result = await self.snapshot(action="callback:cancel", outcome="cancelled")
        self.assertEqual(result["summary"]["nonstart"], 1)
        self.assertEqual(result["summary"]["conversions"], 0)
        self.assertEqual((await self.snapshot(q="%' OR 1=1; --"))["summary"]["registered"], 0)
        self.assertEqual((await self.snapshot(q="user4"))["summary"]["nonstart"], 1)
        self.assertEqual((await self.snapshot(status="failed"))["summary"]["failed"], 1)

    async def test_pagination_and_all_time_cancelled(self):
        result = await self.snapshot(page=2, page_size=2)
        self.assertEqual(len(result["users"]), 2)
        self.assertEqual(len(result["activity"]), 2)
        self.assertEqual(result["pagination"]["activity_total"], 5)
        all_time = await dashboard_data(self.db, {"period": "all"}, 1)
        self.assertEqual(all_time["summary"]["cancelled"], 1)
        self.assertTrue(all("body" not in row and "url" not in row for row in all_time["activity"]))


if __name__ == "__main__":
    unittest.main()
