"""
Discovery report — see every Polymarket temperature market the bot can trade.

    python discover_markets.py            # all cities, today + tomorrow (local), with live METAR
    python discover_markets.py nyc london # just these cities
    python discover_markets.py --sync     # also save the buckets to Supabase (needs bot/.env)

Reads public APIs only (Polymarket Gamma + aviationweather.gov). No keys, no trading.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from weatherbot.catalog import CATALOG, to_station
from weatherbot.exchange.markets import CityDay, GammaClient
from weatherbot.settings import settings
from weatherbot.weather.metar import MetarClient

OK, BAD, OFF = "OK ", "!! ", "-- "


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*", help="city slugs, e.g. nyc london (default: all)")
    ap.add_argument("--sync", action="store_true", help="upsert buckets into Supabase market_buckets")
    ap.add_argument("--no-metar", action="store_true", help="skip the live METAR lookup")
    args = ap.parse_args()

    entries = [e for e in CATALOG if not args.cities or e.city_slug in args.cities]
    unknown_args = set(args.cities) - {e.city_slug for e in CATALOG}
    if unknown_args:
        print(f"Unknown city slug(s): {', '.join(sorted(unknown_args))}")
        print("Known:", ", ".join(e.city_slug for e in CATALOG))
        return 2
    stations = [to_station(e) for e in entries]

    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, timeout=30) as http:
        gamma, metar_client = GammaClient(http), MetarClient(http)
        sem = asyncio.Semaphore(8)

        async def fetch(st, day) -> CityDay:
            async with sem:
                return await gamma.fetch_city_day(
                    city_slug=st.city_slug, target_date=day, unit=st.unit, rules_phrase=st.rules_phrase
                )

        jobs, keys = [], []
        for st in stations:
            today = datetime.now(ZoneInfo(st.timezone)).date()
            for day in (today, today + timedelta(days=1)):
                jobs.append(fetch(st, day))
                keys.append((st.city_slug, day))
        print(f"Checking {len(stations)} cities × 2 days on Polymarket …")
        results = dict(zip(keys, await asyncio.gather(*jobs)))

        obs = {}
        if not args.no_metar:
            try:
                obs = await metar_client.fetch_all(stations)
            except httpx.HTTPError as exc:
                print(f"(METAR lookup failed: {exc})")

        extra = await gamma.search_city_slugs()

    # ------------------------------------------------------------------ report
    print()
    hdr = f"{'':3}{'CITY':<14}{'ICAO':<6}{'DATE':<11}{'BKTS':>4}  {'RANGE':<22}{'METAR HIGH':>11}  {'→ BUCKET NOW':<16}NOTE"
    print(hdr)
    print("-" * len(hdr))
    counts = {"tradeable": 0, "missing": 0, "blocked": 0}
    for st in stations:
        entry = next(e for e in entries if e.city_slug == st.city_slug)
        for day_i, (slug, day) in enumerate(k for k in keys if k[0] == st.city_slug):
            cd = results[(slug, day)]
            o = obs.get(st.icao_code) if day_i == 0 else None
            high = f"{o.high_so_far:.1f}°{st.unit}" if o else ""
            now_bucket = ""
            if o and cd.found:
                b = cd.bucket_for(o.high_so_far)
                now_bucket = b.label if b else "?"
            rng = ""
            if cd.buckets:
                rng = f"{cd.buckets[0].label} … {cd.buckets[-1].label}"
            if not entry.active:
                flag, note = OFF, "not traded (see catalog.py)"
            elif not cd.found:
                flag, note = OFF, cd.problem or "not listed"
                counts["missing"] += 1
            elif cd.rules_ok:
                flag, note = OK, ""
                counts["tradeable"] += 1
            else:
                flag, note = BAD, cd.problem or ""
                counts["blocked"] += 1
            city = st.city_label if day_i == 0 else ""
            icao = st.icao_code if day_i == 0 else ""
            print(f"{flag}{city:<14.14}{icao:<6}{day.isoformat():<11}{len(cd.buckets):>4}  "
                  f"{rng[:22]:<22}{high:>11}  {now_bucket[:16]:<16}{note}")

    print()
    print(f"Tradeable city-days: {counts['tradeable']}   "
          f"not listed yet: {counts['missing']}   "
          f"BLOCKED (rules mismatch): {counts['blocked']}")
    new = sorted(extra - {e.city_slug for e in CATALOG})
    if new:
        print(f"Cities on Polymarket that are NOT in the catalog yet: {', '.join(new)}")

    if args.sync:
        from weatherbot.db import Database
        db = Database()
        rows = await db.get_stations(active_only=False)
        station_ids = {s.city_slug: str(s.id) for s in rows}
        n = await db.upsert_buckets(station_ids, (b for cd in results.values() for b in cd.buckets))
        print(f"Saved {n} buckets to Supabase (market_buckets).")
    return 1 if counts["blocked"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
