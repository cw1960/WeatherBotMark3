"""
WeatherBotMark3 — execution daemon entrypoint.

    cd bot && python main.py

Loop (every config.poll_interval_seconds):
  1. Load active stations + fresh config from Supabase.
  2. For each station, concurrently: METAR latest/daily-high, HRRR high, latest AFD.
  3. Ask Jev System One to read the AFD against the divergence.
  4. Strategy → TradeSignal or skip.
  5. Re-read config.trading_active *immediately* before execution.
     True  → FOK order via polymarket-client.
     False → SIMULATED result, no API call.
  6. Write one trade_audit_logs row per station per cycle.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog

from weatherbot import strategy
from weatherbot.ai.jev import JevClient
from weatherbot.db import Database
from weatherbot.exchange.polymarket import PolymarketExecutor
from weatherbot.models import Station
from weatherbot.settings import settings
from weatherbot.weather.afd import AfdClient
from weatherbot.weather.hrrr import HrrrClient
from weatherbot.weather.metar import MetarClient

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level.upper())
    ),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger("weatherbot")


class Daemon:
    def __init__(self) -> None:
        self.db = Database()
        self.http = httpx.AsyncClient(timeout=30)
        self.metar = MetarClient(self.http)
        self.hrrr = HrrrClient(self.http)
        self.afd = AfdClient(self.http)
        self.jev = JevClient(self.http)
        self.executor = PolymarketExecutor()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        async with self.executor:
            log.info("daemon_start", version="0.1.0")
            while not self._stop.is_set():
                started = loop.time()
                try:
                    interval = await self.cycle()
                except Exception as exc:  # keep the daemon alive on any cycle error
                    log.exception("cycle_failed", error=str(exc))
                    interval = 60
                elapsed = loop.time() - started
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(1, interval - elapsed))
                except asyncio.TimeoutError:
                    pass
            log.info("daemon_stop")
        await self.http.aclose()

    # ---------------------------------------------------------------- cycle
    async def cycle(self) -> int:
        cfg = await self.db.get_config()
        stations = await self.db.get_active_stations()
        cluster_id = str(uuid.uuid4())
        log.info("cycle_start", stations=len(stations), trading_active=cfg.trading_active)

        # Evaluate all stations concurrently; each one is isolated by return_exceptions.
        results = await asyncio.gather(
            *(self.evaluate_station(st, cluster_id) for st in stations),
            return_exceptions=True,
        )
        for st, res in zip(stations, results):
            if isinstance(res, Exception):
                log.error("station_failed", icao=st.icao_code, error=str(res))
        return cfg.poll_interval_seconds

    async def evaluate_station(self, station: Station, cluster_id: str) -> None:
        tz = ZoneInfo(station.timezone)
        now_local = datetime.now(tz)
        target_date = now_local.date()
        day_start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

        # 2. Fetch ground truth, model, and forecaster prose in parallel.
        metar, high_so_far, hrrr, afd = await asyncio.gather(
            self.metar.latest(station.icao_code),
            self.metar.daily_high(station.icao_code, day_start_utc),
            self.hrrr.forecast_high(station, target_date),
            self.afd.latest(station.nws_office) if station.nws_office else asyncio.sleep(0, result=None),
        )

        audit: dict = {
            "station_id": str(station.id),
            "icao_code": station.icao_code,
            "market_id": station.polymarket_market_id,
            "condition_id": station.polymarket_condition_id,
            "target_date": target_date.isoformat(),
            "cluster_id": cluster_id,
            "decision": "skip",
            "trading_active": False,
        }

        if metar is None or hrrr is None:
            audit["skip_reason"] = "missing metar or hrrr data"
            await self.db.log_audit(audit)
            return

        if high_so_far is not None:
            metar.high_so_far_f = max(metar.high_so_far_f, high_so_far)

        audit.update(
            observed_at=metar.observed_at.isoformat(),
            metar_temp_f=metar.temp_f,
            metar_high_so_far_f=metar.high_so_far_f,
            hrrr_high_f=hrrr.high_f,
            divergence_f=round(metar.high_so_far_f - hrrr.high_f, 1),
        )

        # 3. Jev reads the AFD.
        if afd is None:
            audit["skip_reason"] = "no AFD available"
            await self.db.log_audit(audit)
            return
        afd_issued_at, afd_text = afd
        jev = await self.jev.evaluate_afd(
            icao_code=station.icao_code,
            afd_text=afd_text,
            afd_issued_at=afd_issued_at,
            observed_high_f=metar.high_so_far_f,
            hrrr_high_f=hrrr.high_f,
        )
        audit.update(
            jev_sentiment=jev.raw,
            jev_confidence=jev.confidence,
            jev_direction=jev.direction,
            afd_issued_at=afd_issued_at.isoformat(),
            afd_excerpt=jev.afd_excerpt,
        )

        # 4. Strategy. TODO: resolve the correct outcome bucket + token_id from the
        #    market's tokens (Gamma API) based on metar.high_so_far_f.
        outcome_label, token_id = "TODO-bucket", "TODO-token-id"
        market_price = await self.executor.best_ask(token_id) if token_id != "TODO-token-id" else None
        if market_price is None:
            audit["skip_reason"] = "no market price / token mapping not implemented"
            await self.db.log_audit(audit)
            return

        cfg = await self.db.get_config()
        sig, skip_reason = strategy.evaluate(
            station=station, cfg=cfg, metar=metar, hrrr=hrrr, jev=jev,
            target_date=target_date, outcome_label=outcome_label,
            token_id=token_id, market_price=market_price,
        )
        audit.update(market_outcome=outcome_label, market_price=market_price)
        if sig is None:
            audit["skip_reason"] = skip_reason
            await self.db.log_audit(audit)
            return

        audit.update(
            implied_probability=sig.implied_probability,
            model_probability=sig.model_probability,
            edge_pct=sig.edge_pct,
            order_side=sig.side,
            order_type="FOK",
            order_price=sig.price,
            order_size_usdc=sig.size_usdc,
        )

        # 5. THE GATE: fresh read of the kill switch right before touching the API.
        live = (await self.db.get_config()).trading_active
        result = await self.executor.place_fok(sig, live=live)

        # 6. Audit.
        audit.update(
            decision="live" if live else "simulated",
            trading_active=live,
            order_id=result.order_id,
            order_status=result.status,
            fill_size=result.fill_size,
            fill_price=result.fill_price,
            execution_result=result.raw,
            error_message=result.error_message,
        )
        await self.db.log_audit(audit)
        log.info("decision", icao=station.icao_code, decision=audit["decision"], status=result.status)


if __name__ == "__main__":
    asyncio.run(Daemon().run())
