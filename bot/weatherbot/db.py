"""Supabase access layer (service_role — bypasses RLS)."""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from supabase import Client, create_client

from .models import BotConfig, Station
from .settings import settings

log = structlog.get_logger(__name__)


class Database:
    def __init__(self) -> None:
        self._client: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)

    # supabase-py's sync client is wrapped in a thread so the event loop never blocks.
    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ------------------------------------------------------------------ config
    async def get_config(self) -> BotConfig:
        """Fresh read every call — this is the kill switch, never cache it."""
        res = await self._run(
            lambda: self._client.table("config").select("*").eq("id", 1).single().execute()
        )
        return BotConfig.model_validate(res.data)

    # ---------------------------------------------------------------- stations
    async def get_active_stations(self) -> list[Station]:
        res = await self._run(
            lambda: self._client.table("stations").select("*").eq("is_active", True).execute()
        )
        return [Station.model_validate(row) for row in res.data]

    # ------------------------------------------------------------ audit trail
    async def log_audit(self, row: dict[str, Any]) -> None:
        try:
            await self._run(lambda: self._client.table("trade_audit_logs").insert(row).execute())
        except Exception as exc:  # never let audit failures crash the loop
            log.error("audit_insert_failed", error=str(exc), row_keys=list(row))
