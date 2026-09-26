"""
WeatherBotMark3 — execution daemon entrypoint.

    cd bot && python main.py

Every cycle (config.poll_interval_seconds):
  1. Fresh config + all active stations from Supabase.
  2. Every MARKET_REFRESH_SECONDS: discover today's + tomorrow's temperature events for
     every city from the Gamma API, verify each one's rules name our station, and
     upsert every bucket into `market_buckets`.
  3. Every FORECAST_REFRESH_SECONDS: multi-model forecasts (Open-Meteo, ~10 models) and
     airport TAFs for every station.
  4. One METAR request for all stations → latest reading + running local-day high.
  5. Per station (concurrently, bounded): build the evidence bundle (obs + every model +
     spread + TAF + AFD where it exists), ask Jev, pick the bucket for the consensus
     projected high, read its order book.
  6. Strategy → TradeSignal or skip.
  7. Re-read config.trading_active *immediately* before execution.
     True  → FOK order via polymarket-client.   False → SIMULATED, no API call.
  8. One trade_audit_logs row per station per cycle, including the full evidence bundle.
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
from weatherbot.ai.evidence import build_evidence
from weatherbot.ai.jev import JevClient, build_state
from weatherbot.probability import bucket_probabilities, forecast_distribution
from weatherbot.db import Database
from weatherbot.exchange.markets import CityDay, GammaClient
from weatherbot.exchange.polymarket import ORDER_TYPE, PolymarketExecutor
from weatherbot.models import JevEvaluation, MetarObservation, Station
from weatherbot.settings import settings
from weatherbot.weather.afd import AfdClient
from weatherbot.weather.forecast import ForecastClient, StationForecast, model_for
from weatherbot.weather.metar import MetarClient
from weatherbot.weather.taf import Taf, TafClient

MARKET_REFRESH_SECONDS = 15 * 60
FORECAST_REFRESH_SECONDS = 30 * 60
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
        self.taf = TafClient(self.http)
        self.jev = JevClient()
        self._jev_cache: dict[str, tuple[tuple, JevEvaluation]] = {}
        self.executor = PolymarketExecutor()
        self.markets: dict[tuple[str, date], CityDay] = {}
        self._markets_refreshed_at = 0.0
        self.forecasts: dict[tuple[str, date], StationForecast] = {}
        self.tafs: dict[str, Taf] = {}
        self.afds: dict[str, tuple[datetime, str]] = {}
        self._forecasts_refreshed_at = 0.0
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
        await self.jev.aclose()
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

    async def refresh_forecasts(self, stations: list[Station]) -> None:
        fc, tafs = await asyncio.gather(
            self.forecast.fetch_all(stations),
            self.taf.fetch_all([s.icao_code for s in stations]),
            return_exceptions=True,
        )
        if isinstance(fc, Exception):
            log.error("forecast_refresh_failed", error=str(fc))
        else:
            self.forecasts = fc
        if isinstance(tafs, Exception):
            log.error("taf_refresh_failed", error=str(tafs))
        else:
            self.tafs = tafs
        for office in sorted({s.nws_office for s in stations if s.nws_office}):
            try:
                if (afd := await self.afd.latest(office)) is not None:
                    self.afds[office] = afd
            except httpx.HTTPError as exc:
                log.warning("afd_failed", office=office, error=str(exc))
        n_models = sorted({m for f in self.forecasts.values() for m in f.models})
        log.info("forecasts_refreshed", station_days=len(self.forecasts), models=n_models,
                 tafs=len(self.tafs), afds=len(self.afds))

    # ---------------------------------------------------------------- cycle
    async def cycle(self) -> int:
        cfg = await self.db.get_config()
        stations = [s for s in await self.db.get_stations(active_only=True) if s.rules_phrase]
        if time.monotonic() - self._markets_refreshed_at > MARKET_REFRESH_SECONDS:
            await self.refresh_markets(stations)
            self._markets_refreshed_at = time.monotonic()
        if time.monotonic() - self._forecasts_refreshed_at > FORECAST_REFRESH_SECONDS:
            await self.refresh_forecasts(stations)
            self._forecasts_refreshed_at = time.monotonic()

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

        station_fc = self.forecasts.get((station.icao_code, target_date))
        if station_fc is None or not station_fc.models:
            return await skip("no model forecasts for today")
        # divergence: how much warmer (+) / cooler (−) the station has run than the models
        # over the hours already past today. (Comparing the morning's high with the models'
        # afternoon peak would be meaningless.) 0 when it's too early to tell.
        raw_consensus = station_fc.raw_consensus()
        headline = station_fc.projected_highs(metar.high_so_far).get(model_name)
        divergence = station_fc.bias_so_far(metar.high_so_far) or 0.0
        audit.update(
            model_name=f"consensus of {len(station_fc.models)}",
            model_high=raw_consensus,
            secondary_model_high=headline,
            model_spread=station_fc.spread(metar.high_so_far),
            divergence=divergence,
        )

        # Models-only probability for every bucket (code does all the maths).
        buckets = city_day.buckets
        dist0 = forecast_distribution(station_fc, metar.high_so_far, station.unit)
        probs_models_only = bucket_probabilities(dist0, buckets)

        # Jev — every city. The NWS discussion only exists for US offices.
        afd = self.afds.get(station.nws_office) if station.nws_office else None
        taf = self.tafs.get(station.icao_code)
        now = datetime.now(ZoneInfo("UTC"))
        evidence = build_evidence(station=station, metar=metar, forecast=station_fc, taf=taf,
                                  afd=afd, city_day=city_day, now=now)
        jev_state = build_state(station=station, metar=metar, forecast=station_fc, taf=taf,
                                afd=afd, now=now)
        evidence["jev_state"] = jev_state
        audit["evidence"] = evidence
        if afd:
            audit["afd_issued_at"] = afd[0].isoformat()

        # Re-ask Jev only when its inputs change (new METAR, forecasts, TAF or AFD).
        fingerprint = (metar.observed_at, station_fc.fetched_at, taf.issued if taf else None,
                       afd[0] if afd else None, now.astimezone(ZoneInfo(station.timezone)).hour)
        cached = self._jev_cache.get(station.city_slug)
        if cached and cached[0] == fingerprint:
            jev = cached[1]
        else:
            try:
                jev = await self.jev.evaluate(jev_state)
            except Exception as exc:
                return await skip(f"jev unavailable: {exc}")
            self._jev_cache[station.city_slug] = (fingerprint, jev)

        dist = forecast_distribution(station_fc, metar.high_so_far, station.unit,
                                     jev_shift_probs=jev.shift_probs, p_peak_passed=jev.p_peak_passed)
        probs = bucket_probabilities(dist, buckets)
        expected_high = round(sum(k * p for k, p in dist.items()), 1)
        audit.update(
            jev_sentiment=jev.raw, jev_confidence=jev.confidence, jev_direction=jev.direction,
            jev_expected_high=expected_high, afd_excerpt=jev.afd_excerpt,
        )

        # Prices for every bucket in one request.
        quotes = await self.executor.best_asks([b.token_yes for b in buckets])
        asks = {b.label: quotes[b.token_yes][0] for b in buckets if b.token_yes in quotes}

        cfg = await self.db.get_config()
        sig, reason, table = strategy.select_trade(
            station=station, cfg=cfg, target_date=target_date, buckets=buckets,
            probs=probs, probs_models_only=probs_models_only, asks=asks, divergence=divergence,
        )
        evidence["buckets"] = table
        if sig is None:
            return await skip(reason)

        chosen = next(b for b in buckets if b.label == sig.outcome_label)
        audit.update(
            market_id=chosen.market_id, condition_id=chosen.condition_id,
            market_outcome=chosen.label, token_id=chosen.token_yes,
            market_price=sig.market_price,
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
