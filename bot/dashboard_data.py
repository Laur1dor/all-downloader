"""Bounded, read-only statistics for the owner dashboard.

The journal contains action names and results only. No message bodies, URLs,
callback payloads, cache keys or configuration values are returned here.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

_PERIODS = {"all", "24h", "7d", "30d", "90d", "custom"}
_TEXT_FILTERS = ("platform", "status", "media_type", "action", "outcome", "q")


def _utc(value: str, *, end: bool = False) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        result = result.astimezone(timezone.utc)
        if end and len(value) == 10:
            result += timedelta(days=1)
        return result
    except (ValueError, OverflowError):
        raise ValueError("Некорректная дата: используйте ГГГГ-ММ-ДД или ISO 8601") from None


def parse_filters(query: dict, *, now: datetime | None = None) -> dict:
    """Validate query strings. A date-only end includes that whole UTC day."""
    now = now or datetime.now(timezone.utc)
    period = str(query.get("period", "30d"))
    if period not in _PERIODS:
        raise ValueError("Неизвестный период")
    start, end = None, now
    if period == "custom":
        if not query.get("start") or not query.get("end"):
            raise ValueError("Укажите начало и конец периода")
        start = _utc(str(query["start"]))
        end = _utc(str(query["end"]), end=True)
        if start >= end:
            raise ValueError("Конец периода должен быть позже начала")
    elif period != "all":
        start = (
            now - timedelta(hours=24) if period == "24h" else now - timedelta(days=int(period[:-1]))
        )
    try:
        page = int(query.get("page", 1))
        page_size = int(query.get("page_size", 50))
        if not 1 <= page <= 100000 or not 1 <= page_size <= 100:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Недопустимая страница или размер страницы (1–100)") from None
    result = {
        "period": period,
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
        "page": page,
        "page_size": page_size,
    }
    owner = str(query.get("exclude_owner", "true")).lower()
    if owner not in {"true", "false", "1", "0"}:
        raise ValueError("Некорректный фильтр владельца")
    result["exclude_owner"] = owner in {"true", "1"}
    for name in _TEXT_FILTERS:
        value = str(query.get(name, "")).strip()
        if len(value) > 100 or any(ord(char) < 32 for char in value):
            raise ValueError("Слишком длинное или некорректное значение фильтра")
        result[name] = value
    return result


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


# Static SQL, with every user value passed separately. A conversion-only filter
# excludes journal events because they do not carry platform/media attributes;
# an action/outcome filter selects journal events and excludes conversions.
_CTE = """
WITH actors AS (
 SELECT telegram_id FROM users
 UNION SELECT telegram_id FROM conversions
 UNION SELECT telegram_id FROM user_events WHERE action NOT LIKE 'system:%'
), population AS (
 SELECT a.telegram_id, u.username, u.registration_date
 FROM actors a LEFT JOIN users u USING (telegram_id)
 WHERE ($3::bigint IS NULL OR a.telegram_id <> $3)
 AND ($4::text = '' OR strpos(lower(coalesce(u.username,'')), lower($4)) > 0
      OR strpos(a.telegram_id::text, $4) > 0)
), c AS (
 SELECT c.* FROM conversions c JOIN population p USING (telegram_id)
 WHERE ($1::timestamptz IS NULL OR c.created_at >= $1) AND c.created_at < $2
 AND ($5::text = '' OR c.source_platform = $5)
 AND ($6::text = '' OR c.status = $6)
 AND ($7::text = '' OR c.media_type = $7)
 AND $8::text = '' AND $9::text = ''
), e AS (
 SELECT e.* FROM user_events e JOIN population p USING (telegram_id)
 WHERE ($1::timestamptz IS NULL OR e.created_at >= $1) AND e.created_at < $2
 AND e.action NOT LIKE 'system:%'
 AND ($8::text = '' OR e.action = $8) AND ($9::text = '' OR e.outcome = $9)
 AND $5::text = '' AND $6::text = '' AND $7::text = ''
), services AS (
 SELECT e.* FROM user_events e
 WHERE e.action LIKE 'system:%'
 AND ($1::timestamptz IS NULL OR e.created_at >= $1) AND e.created_at < $2
 AND ($8::text = '' OR e.action = $8) AND ($9::text = '' OR e.outcome = $9)
 AND $4::text = '' AND $5::text = '' AND $6::text = '' AND $7::text = ''
), activity AS (
 SELECT c.id, 'conversion'::text AS kind, c.telegram_id, c.created_at,
 'conversion'::text AS action, c.status AS outcome, c.source_platform AS platform,
 c.media_type, c.file_size, (c.processing_time * 1000)::bigint AS duration_ms FROM c
 UNION ALL
 SELECT e.id, 'event', e.telegram_id, e.created_at, e.action, e.outcome,
 NULL::text, NULL::text, NULL::bigint, e.duration_ms::bigint FROM e
 UNION ALL
 SELECT e.id, 'service', e.telegram_id, e.created_at, e.action, e.outcome,
 NULL::text, NULL::text, NULL::bigint, e.duration_ms::bigint FROM services e
), user_counts AS (
 SELECT p.telegram_id, p.username, p.registration_date,
 count(*) FILTER (WHERE a.kind = 'conversion') AS conversions,
 count(*) FILTER (WHERE a.kind = 'event') AS events,
 count(*) FILTER (WHERE a.kind = 'conversion' AND a.outcome = 'done') AS done,
 count(*) FILTER (WHERE a.kind = 'conversion' AND a.outcome = 'failed') AS failed,
 count(*) FILTER (WHERE a.kind = 'conversion' AND a.outcome = 'cancelled') AS cancelled,
 max(a.created_at) AS last_activity
 FROM population p LEFT JOIN activity a USING (telegram_id)
 GROUP BY p.telegram_id, p.username, p.registration_date
)
"""


async def dashboard_data(db, filters: dict, admin_id: int) -> dict:
    """Return one consistent snapshot; abort the whole request after five seconds."""
    # Accept only normalized fields, and validate again at this trust boundary.
    filters = parse_filters(filters)
    start = _utc(filters["start"]) if filters["start"] else None
    end = _utc(filters["end"])
    args = (
        start,
        end,
        admin_id if filters["exclude_owner"] else None,
        filters["q"],
        filters["platform"],
        filters["status"],
        filters["media_type"],
        filters["action"],
        filters["outcome"],
    )
    limit = filters["page_size"]
    offset = (filters["page"] - 1) * limit
    async with asyncio.timeout(5):
        async with (
            db.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            summary = dict(
                await conn.fetchrow(
                    _CTE
                    + """
SELECT (SELECT count(*) FROM population WHERE registration_date < $2) AS registered,
 (SELECT count(DISTINCT telegram_id) FROM activity
  WHERE kind = 'conversion' OR (kind = 'event' AND action <> 'command:start')) AS nonstart,
 (SELECT count(DISTINCT telegram_id) FROM c) AS converters,
 (SELECT count(DISTINCT telegram_id) FROM activity WHERE kind <> 'service') AS active,
 (SELECT count(*) FROM c) AS conversions,
 (SELECT count(*) FROM c WHERE status = 'done') AS done,
 (SELECT count(*) FROM c WHERE status = 'failed') AS failed,
 (SELECT count(*) FROM c WHERE status = 'cancelled') AS cancelled,
 (SELECT coalesce(sum(file_size),0) FROM c WHERE status = 'done') AS bytes,
 (SELECT count(*) FROM e) AS events,
 (SELECT count(*) FROM services) AS service_events,
 (SELECT count(*) FROM activity) AS activity_total,
 (SELECT count(*) FROM user_counts) AS users_total
""",
                    *args,
                    timeout=5,
                )
            )
            series = await conn.fetch(
                _CTE
                + """
SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
 count(*) FILTER (WHERE kind='conversion') AS conversions,
 count(*) FILTER (WHERE kind='conversion' AND outcome='done') AS done,
 count(*) FILTER (WHERE kind='conversion' AND outcome='failed') AS failed,
 count(*) FILTER (WHERE kind='conversion' AND outcome='cancelled') AS cancelled,
 count(*) FILTER (WHERE kind='event') AS events,
 count(*) FILTER (WHERE kind='service') AS service_events
FROM activity GROUP BY day ORDER BY day
""",
                *args,
                timeout=5,
            )
            platforms = await conn.fetch(
                _CTE
                + """
SELECT coalesce(source_platform,'unknown') AS platform, count(*) AS total,
 count(*) FILTER (WHERE status='done') AS done,
 count(*) FILTER (WHERE status='failed') AS failed,
 coalesce(sum(file_size) FILTER (WHERE status='done'),0) AS bytes
FROM c GROUP BY source_platform ORDER BY total DESC, platform
""",
                *args,
                timeout=5,
            )
            users = await conn.fetch(
                _CTE
                + """
SELECT * FROM user_counts ORDER BY last_activity DESC NULLS LAST, telegram_id
LIMIT $10 OFFSET $11
""",
                *args,
                limit,
                offset,
                timeout=5,
            )
            activity = await conn.fetch(
                _CTE
                + """
SELECT a.*, p.username FROM activity a LEFT JOIN users p USING (telegram_id)
ORDER BY created_at DESC, kind, id DESC LIMIT $10 OFFSET $11
""",
                *args,
                limit,
                offset,
                timeout=5,
            )
            uploads = dict(
                await conn.fetchrow(
                    """
SELECT count(*) AS total, coalesce(sum(size_bytes),0) AS bytes,
 coalesce(sum(seconds),0) AS seconds, coalesce(sum(waited_seconds),0) AS waited_seconds,
 CASE WHEN sum(seconds)>0 THEN sum(size_bytes)*8.0/sum(seconds)/1000000 ELSE 0 END AS avg_mbps
FROM upload_samples WHERE ($1::timestamptz IS NULL OR created_at >= $1) AND created_at < $2
 AND (NOT $3::boolean OR NOT is_admin) AND ($4::text='' OR platform=$4)
 AND $5::boolean
""",
                    start,
                    end,
                    filters["exclude_owner"],
                    filters["platform"],
                    not any(
                        filters[key] for key in ("q", "status", "media_type", "action", "outcome")
                    ),
                    timeout=5,
                )
            )
            history = dict(
                await conn.fetchrow(
                    "SELECT (SELECT min(created_at) FROM user_events "
                    "WHERE action NOT LIKE 'system:%') AS events_since, "
                    "(SELECT min(created_at) FROM conversions) AS conversions_since",
                    timeout=5,
                )
            )
            options = await conn.fetch(
                """
SELECT 'platforms' AS name, source_platform AS value FROM conversions
WHERE source_platform IS NOT NULL GROUP BY source_platform
UNION ALL SELECT 'statuses', status FROM conversions GROUP BY status
UNION ALL SELECT 'media_types', media_type FROM conversions
WHERE media_type IS NOT NULL GROUP BY media_type
UNION ALL SELECT 'actions', action FROM user_events GROUP BY action
UNION ALL SELECT 'outcomes', outcome FROM user_events GROUP BY outcome
ORDER BY name, value LIMIT 500
""",
                timeout=5,
            )
    pagination = {
        "page": filters["page"],
        "page_size": limit,
        "users_total": summary.pop("users_total"),
        "activity_total": summary.pop("activity_total"),
    }
    choices = {name: [] for name in ("platforms", "statuses", "media_types", "actions", "outcomes")}
    for row in options:
        choices[row["name"]].append(row["value"])
    return _json(
        {
            "filters": filters,
            "summary": summary,
            "series": [dict(row) for row in series],
            "platforms": [dict(row) for row in platforms],
            "users": [dict(row) for row in users],
            "activity": [dict(row) for row in activity],
            "pagination": pagination,
            "uploads": uploads,
            "options": choices,
            "history": history,
        }
    )
