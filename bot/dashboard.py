"""Owner-only statistics with one-use login tickets and expiring sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from bot.db import Database

logger = logging.getLogger(__name__)
_COOKIE = "stats_session"
_STATIC = Path(__file__).parent / "static" / "dashboard"
_TUNNEL_HOST = re.compile(r"[a-z0-9-]+\.trycloudflare\.com")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _path(kind: str) -> Path:
    return Path(os.getenv(f"DASHBOARD_TUNNEL_{kind.upper()}_FILE",
                          f"data/stats_tunnel_{kind}.json"))


def _write_window(until: datetime) -> None:
    destination = _path("request")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps({"expires_at": until.timestamp()}), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(destination)


def local_url() -> str:
    value = os.getenv("DASHBOARD_LOCAL_URL", "http://localhost:30080/stats/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("DASHBOARD_LOCAL_URL must be an HTTP(S) address")
    return value.rstrip("/") + "/"


def public_url() -> str | None:
    try:
        data = json.loads(_path("status").read_text(encoding="utf-8"))
        parsed = urlsplit(data.get("url", ""))
        if (data.get("state") == "ready" and float(data["expires_at"]) > time.time()
                and parsed.scheme == "https" and _TUNNEL_HOST.fullmatch(parsed.hostname or "")
                and parsed.port is None and parsed.username is None):
            return f"https://{parsed.hostname}/stats/"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


class DashboardAccess:
    def __init__(self, db: Database, admin_id: int):
        self.db = db
        self.admin_id = admin_id

    async def issue(self, minutes: int = 30, *, internet: bool = True) -> dict:
        if minutes not in {15, 30, 60, 120}:
            raise ValueError("Access duration must be 15, 30, 60 or 120 minutes")
        until = _now() + timedelta(minutes=minutes)
        if internet:
            _write_window(until)
        ticket = secrets.token_urlsafe(32)
        await self.db.pool.execute("DELETE FROM dashboard_access WHERE expires_at <= now()")
        await self.db.pool.execute(
            "INSERT INTO dashboard_access(token_hash,kind,expires_at,access_until) "
            "VALUES($1,'ticket',$2,$3)", _hash(ticket), _now() + timedelta(minutes=5), until,
        )
        await self.db.record_event(self.admin_id, "system:stats_access_issued", "ok")
        return {"ticket": ticket, "expires_at": until.isoformat(), "local_url": local_url()}

    async def consume(self, ticket: str) -> tuple[str, datetime] | None:
        if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", ticket):
            return None
        async with self.db.pool.acquire() as connection, connection.transaction():
            until = await connection.fetchval(
                "DELETE FROM dashboard_access WHERE token_hash=$1 AND kind='ticket' "
                "AND expires_at>now() AND access_until>now() RETURNING access_until", _hash(ticket),
            )
            if until is None:
                return None
            token = secrets.token_urlsafe(32)
            await connection.execute(
                "INSERT INTO dashboard_access(token_hash,kind,expires_at,access_until) "
                "VALUES($1,'session',$2,$2)", _hash(token), until,
            )
        await self.db.record_event(self.admin_id, "system:stats_login", "ok")
        return token, until

    async def session(self, token: str) -> datetime | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
            return None
        return await self.db.pool.fetchval(
            "SELECT expires_at FROM dashboard_access WHERE token_hash=$1 "
            "AND kind='session' AND expires_at>now()", _hash(token),
        )

    async def revoke(self) -> None:
        await self.db.pool.execute("DELETE FROM dashboard_access")
        _write_window(_now())
        await self.db.record_event(self.admin_id, "system:stats_access_closed", "ok")


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return request.headers.get("Sec-Fetch-Site") not in {"cross-site"}
    try:
        return urlsplit(origin).netloc.lower() == request.host.lower()
    except ValueError:
        return False


def register_dashboard(app: web.Application, db: Database, admin_id: int) -> None:
    from bot.dashboard_data import dashboard_data, parse_filters

    access = DashboardAccess(db, admin_id)
    attempts: OrderedDict[str, tuple[float, int]] = OrderedDict()
    query_slot = asyncio.Semaphore(2)

    @web.middleware
    async def protect(request, handler):
        if not request.path.startswith("/stats"):
            return await handler(request)
        try:
            host = urlsplit("//" + request.host).hostname
            allowed = {"localhost", "127.0.0.1", "::1", urlsplit(local_url()).hostname}
            public = public_url()
            if public:
                allowed.add(urlsplit(public).hostname)
            if host not in allowed:
                return web.json_response({"error": "Этот адрес панели не разрешён."}, status=403)
            if request.method == "POST" and (
                not _same_origin(request) or request.content_type != "application/json"
            ):
                return web.json_response({"error": "Недопустимый источник запроса."}, status=403)
            if request.content_length and request.content_length > 4096:
                return web.json_response({"error": "Запрос слишком большой."}, status=413)
            response = await handler(request)
        except (ValueError, json.JSONDecodeError):
            response = web.json_response({"error": "Проверьте значения фильтров."}, status=400)
        except (TimeoutError, asyncio.TimeoutError):
            response = web.json_response({"error": "Запрос занял слишком долго. Сузьте период."},
                                         status=504)
        except web.HTTPException as exc:
            response = exc
        except Exception:
            logger.exception("Statistics request failed")
            response = web.json_response({"error": "Не удалось загрузить статистику."}, status=500)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        return response

    app.middlewares.append(protect)

    async def authorized(request):
        until = await access.session(request.cookies.get(_COOKIE, ""))
        if until is None:
            raise web.HTTPUnauthorized(
                text='{"error":"Сессия истекла. Получите ссылку /dashboard."}',
                content_type="application/json",
            )
        return until

    async def login(request):
        peer = request.remote or "unknown"
        began, count = attempts.get(peer, (time.monotonic(), 0))
        if time.monotonic() - began > 60:
            began, count = time.monotonic(), 0
        attempts[peer] = (began, count + 1)
        while len(attempts) > 1024:
            attempts.popitem(last=False)
        if count >= 10:
            return web.json_response({"error": "Слишком много попыток. Подождите минуту."},
                                     status=429)
        payload = await request.json()
        result = await access.consume(payload.get("ticket") if isinstance(payload, dict) else None)
        if result is None:
            return web.json_response(
                {"error": "Ссылка истекла или уже использована. Запросите /dashboard."}, status=401,
            )
        token, until = result
        response = web.json_response({"expires_at": until.isoformat()})
        response.set_cookie(
            _COOKIE, token, max_age=max(1, int((until - _now()).total_seconds())),
            httponly=True, samesite="Strict", path="/stats",
            secure=request.secure or (
                urlsplit("//" + request.host).hostname or ""
            ).endswith(".trycloudflare.com"),
        )
        return response

    async def session(request):
        until = await authorized(request)
        return web.json_response({"expires_at": until.isoformat(), "local_url": local_url(),
                                  "public_available": public_url() is not None})

    async def data(request):
        await authorized(request)
        async with asyncio.timeout(15), query_slot:
            result = await dashboard_data(db, parse_filters(request.query), admin_id)
        return web.json_response(result)

    async def logout(request):
        await authorized(request)
        await access.revoke()
        response = web.json_response({"closed": True})
        response.del_cookie(_COOKIE, path="/stats")
        return response

    async def index(_request):
        return web.FileResponse(_STATIC / "index.html")

    async def asset(request):
        name = request.match_info["asset"]
        if name not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(_STATIC / name)

    async def redirect(_request):
        raise web.HTTPFound("/stats/")

    app.router.add_get("/stats", redirect)
    app.router.add_get("/stats/", index)
    app.router.add_post("/stats/api/login", login)
    app.router.add_get("/stats/api/session", session)
    app.router.add_get("/stats/api/data", data)
    app.router.add_post("/stats/api/logout", logout)
    app.router.add_get("/stats/{asset}", asset)
