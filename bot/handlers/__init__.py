"""Aiogram routers. Inclusion order matters: the catch-all fallback goes last."""

from __future__ import annotations

from aiogram import Router

from bot.handlers import admin, download, user


def create_root_router(admin_id: int) -> Router:
    root = Router(name="root")
    root.include_router(admin.create_router(admin_id))
    root.include_router(user.router)
    root.include_router(download.router)
    root.include_router(user.fallback_router)
    return root
