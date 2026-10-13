"""Bounded, read-only operational analytics over the existing PostgreSQL data."""

from __future__ import annotations

import csv
import html
from io import StringIO
from typing import Any

from bot.db import Database

PERIODS = {"24h": 24, "7d": 168, "30d": 720}
_EXPORT_LIMIT = 5000
_MAX_EXPORT_BYTES = 7_000_000

_AUDIENCE_SQL = """
WITH active AS (
    SELECT DISTINCT telegram_id FROM user_events
    WHERE created_at >= now() - ($1::int * interval '1 hour')
)
SELECT
    (SELECT count(*) FROM active) AS active_users,
    (SELECT count(*) FROM users
     WHERE registration_date >= now() - ($1::int * interval '1 hour')) AS new_users,
    (SELECT count(*) FROM active AS a JOIN users AS u USING (telegram_id)
     WHERE u.registration_date < now() - ($1::int * interval '1 hour')) AS returning_users
"""
_CONVERSIONS_SQL = """
SELECT coalesce(source_platform, 'other') AS platform, status,
       count(*) AS total,
       round(avg(processing_time)::numeric, 1) AS avg_seconds,
       coalesce(sum(file_size), 0) AS bytes
FROM conversions
WHERE created_at >= now() - ($1::int * interval '1 hour')
GROUP BY 1, 2 ORDER BY total DESC
"""
_EVENTS_SQL = """
SELECT action, outcome, count(*) AS total,
       round(avg(duration_ms)::numeric) AS avg_ms
FROM user_events
WHERE created_at >= now() - ($1::int * interval '1 hour')
GROUP BY action, outcome ORDER BY total DESC LIMIT 15
"""
_UPLOADS_SQL = """
SELECT count(*) AS total, coalesce(sum(size_bytes), 0) AS bytes,
       round(avg(waited_seconds)::numeric, 1) AS avg_wait,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY waited_seconds))::numeric, 1)
           AS p95_wait,
       round(avg(seconds)::numeric, 1) AS avg_seconds
FROM upload_samples
WHERE created_at >= now() - ($1::int * interval '1 hour')
"""
_RECENT_EVENTS_SQL = """
SELECT created_at, telegram_id, action, outcome, duration_ms
FROM user_events WHERE created_at >= now() - ($1::int * interval '1 hour')
ORDER BY created_at DESC, id DESC LIMIT $2
"""
_RECENT_CONVERSIONS_SQL = """
SELECT created_at, telegram_id, source_platform, media_type, status,
       file_size, processing_time
FROM conversions WHERE created_at >= now() - ($1::int * interval '1 hour')
ORDER BY created_at DESC, id DESC LIMIT $2
"""
_RECENT_UPLOADS_SQL = """
SELECT created_at, platform, lane, is_admin, size_bytes, seconds,
       waited_seconds, alongside
FROM upload_samples WHERE created_at >= now() - ($1::int * interval '1 hour')
ORDER BY created_at DESC, id DESC LIMIT $2
"""


def _number(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}" if digits else str(int(value))


def _safe(value: Any) -> str:
    return html.escape(str(value)[:48], quote=False)


async def summary(db: Database, period: str = "24h") -> str:
    """Human sized summary; all counts are scoped to a supported period."""
    hours = PERIODS[period]
    audience = await db.pool.fetchrow(_AUDIENCE_SQL, hours)
    conversions = await db.pool.fetch(_CONVERSIONS_SQL, hours)
    events = await db.pool.fetch(_EVENTS_SQL, hours)
    uploads = await db.pool.fetchrow(_UPLOADS_SQL, hours)
    done = sum(row["total"] for row in conversions if row["status"] == "done")
    failed = sum(row["total"] for row in conversions if row["status"] == "failed")
    cancelled = sum(row["total"] for row in conversions if row["status"] == "cancelled")
    lines = [
        f"📊 <b>Аналитика за {period}</b>",
        (f"👥 Активных: <b>{audience['active_users']}</b> · новых: "
         f"<b>{audience['new_users']}</b> · вернувшихся: "
         f"<b>{audience['returning_users']}</b>"),
        (f"🎬 Загрузок: <b>{done}</b> успешных · {failed} ошибок · "
         f"{cancelled} отмен · "
         f"{_number(sum(row['bytes'] for row in conversions) / 1048576, 1)} МБ"),
        "\n<b>Сервисы</b>",
    ]
    if conversions:
        lines.extend(
            f"• {_safe(row['platform'])} / {_safe(row['status'])}: {row['total']} · "
            f"среднее {_number(row['avg_seconds'], 1)} с"
            for row in conversions[:12]
        )
    else:
        lines.append("• Пока нет загрузок")
    lines.append("\n<b>Действия и кнопки</b>")
    if events:
        lines.extend(
            f"• {_safe(row['action'])} / {_safe(row['outcome'])}: {row['total']}"
            for row in events[:10]
        )
    else:
        lines.append("• Пока нет событий")
    lines.extend([
        "\n<b>Отправка файлов</b>",
        (f"• {uploads['total']} отправок · ожидание в среднем "
         f"{_number(uploads['avg_wait'], 1)} с, p95 {_number(uploads['p95_wait'], 1)} с"),
        (f"• Передано {_number((uploads['bytes'] or 0) / 1048576, 1)} МБ · "
         f"среднее время {_number(uploads['avg_seconds'], 1)} с"),
        "\n<code>/analytics_export 7d</code> — подробный CSV (макс. 5000 записей каждого типа).",
    ])
    return "\n".join(lines)


def _cell(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def export_csv(db: Database, period: str = "7d") -> bytes:
    """Recent raw operational rows, excluding message text and callback data."""
    hours = PERIODS[period]
    output = StringIO(newline="")
    writer = csv.writer(output)
    sections = (
        ("events", _RECENT_EVENTS_SQL,
         ("created_at", "telegram_id", "action", "outcome", "duration_ms")),
        ("conversions", _RECENT_CONVERSIONS_SQL,
         ("created_at", "telegram_id", "source_platform", "media_type", "status",
          "file_size", "processing_time")),
        ("uploads", _RECENT_UPLOADS_SQL,
         ("created_at", "platform", "lane", "is_admin", "size_bytes", "seconds",
          "waited_seconds", "alongside")),
    )
    for title, query, columns in sections:
        writer.writerow((title,))
        writer.writerow(columns)
        rows = await db.pool.fetch(query, hours, _EXPORT_LIMIT)
        for row in rows:
            writer.writerow([_cell(row[column]) for column in columns])
            if output.tell() >= _MAX_EXPORT_BYTES:
                writer.writerow(("truncated_at_size_limit",))
                return output.getvalue().encode("utf-8-sig")
        writer.writerow(())
    return output.getvalue().encode("utf-8-sig")
