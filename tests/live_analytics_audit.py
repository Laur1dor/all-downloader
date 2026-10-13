"""Read-only check of analytics against the deployed database."""

from __future__ import annotations

import asyncio

from bot.analytics import export_csv, summary
from bot.config import load_settings
from bot.db import Database


async def main() -> None:
    db = Database(load_settings().database_dsn)
    await db.connect()
    try:
        for period in ("24h", "7d", "30d"):
            report = await summary(db, period)
            export = await export_csv(db, period)
            assert 0 < len(report) < 4096
            assert 0 < len(export) < 7_000_000
            print(f"{period}: report={len(report)} chars, CSV={len(export)} bytes")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
