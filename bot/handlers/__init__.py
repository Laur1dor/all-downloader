"""Aiogram routers. Inclusion order matters: the catch-all fallback goes last."""

from __future__ import annotations

from aiogram import Router

from bot.handlers import admin, download, user
from bot.handlers.flood import FloodMiddleware


def create_root_router(admin_id: int) -> Router:
    root = Router(name="root")
    # In front of everything, so a message over the line never reaches a handler
    # and refusing it stays cheap. Callbacks are included: a button held down is
    # a flood like any other.
    flood = FloodMiddleware(admin_id)
    root.message.middleware(flood)
    root.callback_query.middleware(flood)
    root.include_router(admin.create_router(admin_id))
    root.include_router(user.router)
    root.include_router(download.router)
    root.include_router(user.fallback_router)
    return root
