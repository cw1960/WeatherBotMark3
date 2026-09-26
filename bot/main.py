"""
WeatherBotMark3 — execution daemon entrypoint.

    cd bot && python main.py

Every cycle (config.poll_interval_seconds):
  1. Fresh config + all active stations from Supabase.
  2. Every MARKET_REFRESH_SECONDS: discover today's + tomorrow's temperature events for
     every city from the Gamma API, verify each one's rules name our station, and
     upsert every bucket into `market_buckets`.
  3. One METAR request for all stations → latest reading + running local-day high.
  4. Per station (concurrently, bounded): model forecast, AFD (US only), Jev read,
     pick the bucket the projected high falls in, read its order book.
  5. Strategy → TradeSignal or skip.
  6. Re-read config.trading_active *immediately* before execution.
     True  → FOK order via polymarket-client.   False → SIMULATED, no API call.
  7. One trade_audit_logs row per station per cycle.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import structlog

from weatherbot import strategy
from weatherbot.ai.jev import JevClient
from weatherbot.db import Database
from weatherbot.exchange.markets import CityDay, GammaClient
from weatherbot.exchange.polymarket import ORDER_TYPE, PolymarketExecutor
from weatherbot.models import MetarObservation, Station
from weatherbot.settings import settings
from weatherbot.weather.afd import AfdClient
from weatherbot.weather.forecast import ForecastClient, model_for
from weatherbot.weather.metar import MetarClient

MARKET_REFRESH_SECONDS = 15 * 60
MAX_CONCURRENT_STATIONS = 8

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


def local_today(st: Station) -> date:
    return datetime.now(ZoneInfo(st.timezone)).date()


class Daemon:
    def __init__(self) -> None:
        self.db = Database()
        self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": settings.user_agent})
        self.gamma = GammaClient(self.http)
        self.metar = MetarClient(self.http)
        self.forecast = ForecastClient(self.http)
        self.afd = AfdClient(self.http)
        self.jev = JevClient(self.http)
        self.executor = PolymarketExecutor()
        self.markets: dict[tuple[str, date], CityDay] = {}
        self._markets_refreshed_at = 0.0
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_STATIONS)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        async with self.executor:
            log.info("daemon_start", version="0.2.0", order_type=ORDER_TYPE)
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

    # ------------------------------------------------------- market refresh
    async def refresh_markets(self, stations: list[Station]) -> None:
        async def one(st: Station, day: date) -> CityDay:
            async with self._sem:
                return await self.gamma.fetch_city_day(
                    city_slug=st.city_slug, target_date=day, unit=st.unit, rules_phrase=st.rules_phrase
                )

        jobs = []
        for st in stations:
            today = local_today(st)
            jobs += [one(st, today), one(st, today + timedelta(days=1))]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        fresh: dict[tuple[str, date], CityDay] = {}
        for res in results:
            if isinstance(res, Exception):
                log.error("market_fetch_failed", error=str(res))
                continue
            fresh[(res.city_slug, res.target_date)] = res
            if res.found and not res.rules_ok:
                log.warning("market_not_tradeable", city=res.city_slug, date=str(res.target_date),
                            problem=res.problem)
        self.markets = fresh

        station_ids = {st.city_slug: str(st.id) for st in stations}
        n = await self.db.upsert_buckets(
            station_ids, (b for cd in fresh.values() for b in cd.buckets)
        )
        tradeable = sum(1 for cd in fresh.values() if cd.rules_ok)
        log.info("markets_refreshed", city_days=len(fresh), tradeable=tradeable, buckets=n)

        known = {st.city_slug for st in stations}
        unknown = await self.gamma.search_city_slugs() - known
        if unknown:
            log.warning("uncatalogued_cities", slugs=sorted(unknown),
                        hint="add them to weatherbot/catalog.py after checking their rules")

    # ---------------------------------------------------------------- cycle
    async def cycle(self) -> int:
        cfg = await self.db.get_config()
        stations = [s for s in await self.db.get_stations(active_only=True) if s.rules_phrase]
        if time.monotonic() - self._markets_refreshed_at > MARKET_REFRESH_SECONDS:
            await self.refresh_markets(stations)
            self._markets_refreshed_at = time.monotonic()

        observations = await self.metar.fetch_all(stations)
        cluster_id = str(uuid.uuid4())
        log.info("cycle_start", stations=len(stations), with_metar=len(observations),
                 trading_active=cfg.trading_active)

        async def guarded(st: Station) -> None:
            async with self._sem:
                await self.evaluate_station(st, observations.get(st.icao_code), cluster_id)

        results = await asyncio.gather(*(guarded(st) for st in stations), return_exceptions=True)
        for st, res in zip(stations, results):
            if isinstance(res, Exception):
                log.error("station_failed", city=st.city_slug, error=str(res))
        return cfg.poll_interval_seconds

    async def evaluate_station(
        self, station: Station, metar: MetarObservation | None, cluster_id: str
    ) -> None:
        target_date = local_today(station)
        city_day = self.markets.get((station.city_slug, target_date))
        model_name = model_for(station)

        audit: dict = {
            "station_id": str(station.id),
            "city_slug": station.city_slug,
            "icao_code": station.icao_code,
            "market_id": "",
            "condition_id": "",
            "target_date": target_date.isoformat(),
            "unit": station.unit,
            "model_name": model_name,
            "cluster_id": cluster_id,
            "decision": "skip",
            "trading_active": False,
        }

        async def skip(reason: str) -> None:
            audit["skip_reason"] = reason
            await self.db.log_audit(audit)

        if city_day is None or not city_day.found:
            return await skip("no Polymarket event listed for today")
        if not city_day.rules_ok:
            return await skip(f"market not tradeable: {city_day.problem}")
        if metar is None:
            return await skip("no METAR since local midnight")

        audit.update(
            observed_at=metar.observed_at.isoformat(),
            obs_temp=metar.temp,
            obs_high_so_far=metar.high_so_far,
        )

        forecast = await self.forecast.forecast_high(station, target_date)
        if forecast is None:
            return await skip(f"no {model_name} forecast (forecast feed not implemented yet)")
        audit.update(model_high=forecast.high, divergence=round(metar.high_so_far - forecast.high, 1))

        # The day's high can only rise from here, so project the final high as the
        # larger of what's already been observed and what the model expects.
        # TODO: replace with a proper distribution over buckets.
        projected = max(metar.high_so_far, forecast.high)
        bucket = city_day.bucket_for(projected)
        if bucket is None or bucket.closed:
            return await skip(f"no open bucket for projected high {projected}°{station.unit}")
        audit.update(market_id=bucket.market_id, condition_id=bucket.condition_id,
                     market_outcome=bucket.label, token_id=bucket.token_yes)

        # Jev reads the NWS Area Forecast Discussion — only exists for US offices.
        if not station.nws_office:
            return await skip("no AFD source outside the US (Jev gate) — decision pending")
        afd = await self.afd.latest(station.nws_office)
        if afd is None:
            return await skip("no AFD available")
        afd_issued_at, afd_text = afd
        jev = await self.jev.evaluate_afd(
            icao_code=station.icao_code, afd_text=afd_text, afd_issued_at=afd_issued_at,
            observed_high=metar.high_so_far, model_high=forecast.high,
            model_name=model_name, unit=station.unit,
        )
        audit.update(
            jev_sentiment=jev.raw, jev_confidence=jev.confidence, jev_direction=jev.direction,
            afd_issued_at=afd_issued_at.isoformat(), afd_excerpt=jev.afd_excerpt,
        )

        quote = await self.executor.best_ask(bucket.token_yes)
        if quote is None:
            return await skip("empty order book")
        market_price, _ask_size = quote
        audit["market_price"] = market_price

        cfg = await self.db.get_config()
        sig, skip_reason = strategy.evaluate(
            station=station, cfg=cfg, metar=metar, forecast=forecast, jev=jev,
            target_date=target_date, outcome_label=bucket.label,
            token_id=bucket.token_yes, market_price=market_price,
        )
        if sig is None:
            return await skip(skip_reason or "strategy declined")

        audit.update(
            implied_probability=sig.implied_probability,
            model_probability=sig.model_probability,
            edge_pct=sig.edge_pct,
            order_side=sig.side,
            order_type=ORDER_TYPE,
            order_price=sig.price,
            order_size_usdc=sig.size_usdc,
        )

        # THE GATE: fresh read of the kill switch right before touching the API.
        live = (await self.db.get_config()).trading_active
        result = await self.executor.place_fok(sig, live=live)

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
        log.info("decision", city=station.city_slug, decision=audit["decision"], status=result.status)


if __name__ == "__main__":
    asyncio.run(Daemon().run())
