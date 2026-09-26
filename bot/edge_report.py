"""
Edge report — what every forecast model says about today's high, city by city.

    python edge_report.py              # summary table for all active cities
    python edge_report.py nyc tokyo    # just these cities
    python edge_report.py nyc --detail # every model + the airport forecast (TAF) for one city

Reads public APIs only (Polymarket, aviationweather.gov, Open-Meteo). No keys, no trading.
Columns: OBS HIGH = highest reading so far today; MODELS SAID = median of the models'
full-day forecasts; DIFF = how much hotter (+) or colder (−) the station is running than
modelled so far; FINAL EST = median projected final high; SPREAD = model disagreement.

Cities where the models point at DIFFERENT buckets are flagged: that disagreement is where
market prices are most often wrong, and where Jev's judgment matters most.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from weatherbot.ai.evidence import build_evidence
from weatherbot.catalog import CATALOG, to_station
from weatherbot.exchange.markets import GammaClient
from weatherbot.settings import settings
from weatherbot.weather.forecast import ForecastClient
from weatherbot.weather.metar import MetarClient
from weatherbot.weather.taf import TafClient


def fmt(v, unit=""):
    return "—" if v is None else f"{v:.1f}{unit}"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*", help="city slugs (default: all active)")
    ap.add_argument("--detail", action="store_true", help="show every model and the TAF")
    args = ap.parse_args()

    if args.cities:
        entries = [e for e in CATALOG if e.city_slug in args.cities]
    else:
        entries = [e for e in CATALOG if e.active]
    unknown = set(args.cities) - {e.city_slug for e in CATALOG}
    if unknown:
        print(f"Unknown city slug(s): {', '.join(sorted(unknown))}")
        return 2
    stations = [to_station(e) for e in entries]
    now = datetime.now(timezone.utc)

    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, timeout=45) as http:
        gamma = GammaClient(http)
        sem = asyncio.Semaphore(8)

        async def market(st):
            async with sem:
                day = now.astimezone(ZoneInfo(st.timezone)).date()
                return st.city_slug, await gamma.fetch_city_day(
                    city_slug=st.city_slug, target_date=day, unit=st.unit, rules_phrase=st.rules_phrase)

        print(f"Fetching observations, ~10 forecast models, airport forecasts and markets "
              f"for {len(stations)} cities …")
        obs, forecasts, tafs, markets = await asyncio.gather(
            MetarClient(http).fetch_all(stations),
            ForecastClient(http).fetch_all(stations, now=now),
            TafClient(http).fetch_all([s.icao_code for s in stations]),
            asyncio.gather(*(market(s) for s in stations)),
        )
        markets = dict(markets)

    print()
    hdr = (f"{'CITY':<14}{'LOCAL':>6}  {'OBS HIGH':>9}{'MODELS SAID':>12}{'DIFF':>7}{'FINAL EST':>10}"
           f"{'SPREAD':>8}{'MODELS':>7}  {'TAF MAX':>8}  {'BUCKET (final est)':<20}{'MODELS AGREE?'}")
    print(hdr)
    print("-" * len(hdr))
    flagged = 0
    for st in stations:
        tz = ZoneInfo(st.timezone)
        day = now.astimezone(tz).date()
        o = obs.get(st.icao_code)
        fc = forecasts.get((st.icao_code, day))
        taf = tafs.get(st.icao_code)
        cd = markets.get(st.city_slug)
        ev = build_evidence(station=st, metar=o, forecast=fc, taf=taf, afd=None, city_day=cd, now=now)
        u = f"°{st.unit}"
        high = o.high_so_far if o else None
        cons = fc.consensus(high) if fc else None
        spread = fc.spread(high) if fc else None
        said = fc.raw_consensus() if fc else None
        diff = round(high - said, 1) if (high is not None and said is not None) else None
        tx = (ev.get("taf") or {}).get("explicit_max")
        bucket = cd.bucket_for(cons).label if (cd and cons is not None and cd.bucket_for(cons)) else "—"
        distinct = (ev.get("market") or {}).get("distinct_buckets_across_models", 0)
        agree = "yes" if distinct == 1 else (f"NO — {distinct} buckets" if distinct > 1 else "—")
        if distinct > 1:
            flagged += 1
        print(f"{st.city_label[:13]:<14}{now.astimezone(tz).strftime('%H:%M'):>6}  "
              f"{fmt(high, u):>9}{fmt(said, u):>12}{('—' if diff is None else f'{diff:+.1f}'):>7}{fmt(cons, u):>10}"
              f"{fmt(spread):>8}{(len(fc.models) if fc else 0):>7}  "
              f"{fmt(tx['max_temp'] if tx else None, u):>8}  {bucket[:19]:<20}{agree}")

        if args.detail:
            if fc:
                print(f"    {'model':<16}{'full-day high':>14}{'still to come':>15}{'hrs left':>9}{'projected':>11}  bucket")
                proj = fc.projected_highs(high)
                per = (ev.get("market") or {}).get("bucket_per_model", {})
                for name, m in sorted(fc.models.items(), key=lambda kv: proj[kv[0]]):
                    print(f"    {name:<16}{fmt(m.full_day_high, u):>14}{fmt(m.remaining_high, u):>15}"
                          f"{m.hours_remaining:>9}{fmt(proj[name], u):>11}  {per.get(name) or '—'}")
            if o:
                print(f"    latest METAR: {o.raw}")
            if taf:
                print(f"    TAF: {taf.raw}")
            print()

    print()
    print(f"Cities where the models disagree on the bucket: {flagged} of {len(stations)}")
    if not settings.open_meteo_api_key:
        print("(Using Open-Meteo's free, non-commercial endpoint. Set OPEN_METEO_API_KEY before live trading.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
