"""Supabase access layer (service_role — bypasses RLS)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

import structlog
from supabase import Client, create_client

from .exchange.markets import Bucket
from .models import BotConfig, Station
from .settings import settings

log = structlog.get_logger(__name__)


class Database:
    def __init__(self) -> None:
        settings.require("supabase_url", "supabase_service_role_key")
        self._client: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)

    # supabase-py's sync client is wrapped in a thread so the event loop never blocks.
    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    # ------------------------------------------------------------------ config
    async def get_config(self) -> BotConfig:
        """Fresh read every call — this is the kill switch, never cache it."""
        res = await self._run(
            lambda: self._client.table("config").select("*").eq("id", 1).single().execute()
        )
        return BotConfig.model_validate(res.data)

    # ---------------------------------------------------------------- stations
    async def get_stations(self, *, active_only: bool = True) -> list[Station]:
        def q():
            query = self._client.table("stations").select("*")
            if active_only:
                query = query.eq("is_active", True)
            return query.order("city_slug").execute()
        res = await self._run(q)
        return [Station.model_validate(row) for row in res.data]

    # ---------------------------------------------------------- market buckets
    async def upsert_buckets(self, station_ids: dict[str, str], buckets: Iterable[Bucket]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "station_id": station_ids[b.city_slug],
                "target_date": b.target_date.isoformat(),
                "event_id": b.event_id,
                "event_slug": b.event_slug,
                "market_id": b.market_id,
                "condition_id": b.condition_id,
                "label": b.label,
                "lower_bound": b.lower,
                "upper_bound": b.upper,
                "unit": b.unit,
                "token_yes": b.token_yes,
                "token_no": b.token_no,
                "neg_risk": b.neg_risk,
                "rules_ok": b.rules_ok,
                "closed": b.closed,
                "updated_at": now,
            }
            for b in buckets if b.city_slug in station_ids
        ]
        if not rows:
            return 0
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            await self._run(
                lambda c=chunk: self._client.table("market_buckets")
                .upsert(c, on_conflict="condition_id").execute()
            )
        return len(rows)

    # ------------------------------------------------------------ audit trail
    async def log_audit(self, row: dict[str, Any]) -> None:
        try:
            await self._run(lambda: self._client.table("trade_audit_logs").insert(row).execute())
        except Exception as exc:  # never let audit failures crash the loop
            log.error("audit_insert_failed", error=str(exc), row_keys=list(row))
