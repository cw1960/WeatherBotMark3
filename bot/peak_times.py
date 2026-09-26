"""
Peak-time study — WHEN does each city's daily high happen?

    python peak_times.py              # last 60 days, all active cities
    python peak_times.py --days 90    # longer history
    python peak_times.py nyc tokyo    # just these cities

Idea (Christopher): the high almost always comes in the afternoon within a narrow window, and
trades must be placed BEFORE it — so calibrate the whole system around each city's peak time.

For each city and each past local day, from archived airport readings (Iowa Environmental
Mesonet ASOS/METAR archive, hourly routine + special reports):
  * the time the day's high was FIRST reached (after that, the answer is on the board)
  * typical peak time (median) and the 80% window (10th–90th percentile)
  * share of days within ±1 h of the typical time (your "2-hour window")
  * share of odd days (peak before 10:00 or at/after 19:00 — fronts, sea breezes, night warmth)
  * how often the high is already locked in by 13:00 / 14:00 / 15:00 local
  * data check: does the archive's daily high land in the bucket Polymarket actually settled?
    (uses the settled markets already downloaded by backtest.py, if present)

Saves bot/calibration/peak_times.json (used next to schedule each city's decision times).
Downloads are cached in bot/.backtest_cache/iem/ and requests are spaced to respect the archive.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import floor
from pathlib import Path

import httpx

from weatherbot.catalog import CATALOG, to_station
from weatherbot.models import Station
from weatherbot.settings import settings

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".backtest_cache"
OUT = HERE / "calibration" / "peak_times.json"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
MIN_OBS_PER_DAY = 18
ODD_BEFORE, ODD_FROM = 10 * 60, 19 * 60     # minutes after local midnight


def iem_id(st: Station) -> str:
    """IEM uses 3-letter FAA ids for US airports (LGA), ICAO elsewhere (EGLC)."""
    return st.icao_code[1:] if st.country == "US" and st.icao_code.startswith("K") else st.icao_code


def parse_iem_csv(text: str) -> list[tuple[datetime, float]]:
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        v, t = rec.get("tmpc"), rec.get("valid")
        if not v or v == "M" or not t:
            continue
        try:
            rows.append((datetime.strptime(t, "%Y-%m-%d %H:%M"), float(v)))
        except ValueError:
            continue
    return rows


def to_unit(c: float, unit: str) -> float:
    return c * 9 / 5 + 32 if unit == "F" else c


def daily_peaks(rows: list[tuple[datetime, float]], unit: str) -> dict[date, tuple[int, float]]:
    """Local day → (minutes after midnight when the day's max was FIRST reached, max in unit)."""
    per_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for t, c in rows:
        per_day[t.date()].append((t, c))
    out = {}
    for d, obs in per_day.items():
        if len(obs) < MIN_OBS_PER_DAY:
            continue
        obs.sort()
        hi = max(c for _, c in obs)
        first = next(t for t, c in obs if c >= hi - 0.05)
        out[d] = (first.hour * 60 + first.minute, round(to_unit(hi, unit), 1))
    return out


def hhmm(m: float) -> str:
    m = int(round(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(floor(k)), min(int(floor(k)) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarise(peaks: dict[date, tuple[int, float]]) -> dict | None:
    if len(peaks) < 10:
        return None
    mins = [m for m, _ in peaks.values()]
    med = statistics.median(mins)
    return {
        "n_days": len(mins),
        "median_peak": hhmm(med), "median_peak_min": round(med),
        "p10": hhmm(pct(mins, 0.1)), "p90": hhmm(pct(mins, 0.9)),
        "within_1h_of_median": round(sum(abs(m - med) <= 60 for m in mins) / len(mins), 3),
        "odd_days": round(sum(m < ODD_BEFORE or m >= ODD_FROM for m in mins) / len(mins), 3),
        "locked_by_13": round(sum(m <= 13 * 60 for m in mins) / len(mins), 3),
        "locked_by_14": round(sum(m <= 14 * 60 for m in mins) / len(mins), 3),
        "locked_by_15": round(sum(m <= 15 * 60 for m in mins) / len(mins), 3),
    }


def settled_bucket(st: Station, d: date):
    """(lo, hi) of the bucket Polymarket settled on, from backtest.py's cache (or None)."""
    from weatherbot.exchange.markets import event_slug
    import backtest as bt
    p = CACHE / "gamma" / f"{event_slug(st.city_slug, d)}.json"
    if not p.exists():
        return None
    parsed = bt.parse_settled_event(json.loads(p.read_text())[0], st.unit)
    if not parsed:
        return None
    buckets, winner = parsed
    b = next(b for b in buckets if b.label == winner)
    return b.lower, b.upper


async def fetch_iem(http: httpx.AsyncClient, st: Station, start: date, end: date) -> str | None:
    key = f"{iem_id(st)}_{start}_{end}"
    p = CACHE / "iem" / f"{key}.csv"
    if p.exists():
        return p.read_text()
    e = end + timedelta(days=1)
    params = [("station", iem_id(st)), ("data", "tmpc"), ("tz", st.timezone),
              ("year1", start.year), ("month1", start.month), ("day1", start.day),
              ("year2", e.year), ("month2", e.month), ("day2", e.day),
              ("format", "onlycomma"), ("latlon", "no"), ("missing", "M"),
              ("report_type", "3"), ("report_type", "4")]
    for attempt in range(5):
        try:
            r = await http.get(IEM, params=params, timeout=120)
        except httpx.TransportError:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:
            await asyncio.sleep(15 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text)
        return r.text
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()
    entries = [e for e in CATALOG if (e.city_slug in args.cities if args.cities else e.active)]
    stations = [to_station(e) for e in entries]
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=args.days - 1)
    print(f"Peak-time study {start} → {end} for {len(stations)} cities (archive requests are spaced out; "
          f"first run takes a few minutes).")

    results, rows_out = {}, []
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as http:
        for i, st in enumerate(stations, 1):
            text = await fetch_iem(http, st, start, end)
            if text is None:
                print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} ! archive unavailable")
                continue
            peaks = daily_peaks(parse_iem_csv(text), st.unit)
            summ = summarise(peaks)
            if summ is None:
                print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} ! too few complete days ({len(peaks)})")
                continue
            # data check against Polymarket's settled bucket
            agree = total = 0
            for d, (_, hi) in peaks.items():
                sb = settled_bucket(st, d)
                if sb is None:
                    continue
                lo, up = sb
                r = int(floor(hi + 0.5))
                total += 1
                agree += (lo is None or r >= lo) and (up is None or r <= up)
            summ["archive_matches_settlement"] = round(agree / total, 3) if total else None
            summ["checked_days"] = total
            results[st.icao_code] = {"city": st.city_label, "timezone": st.timezone, **summ}
            rows_out.append((st, summ))
            print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} days={summ['n_days']}")
            await asyncio.sleep(1.5)   # be polite to the archive

    print()
    hdr = (f"{'CITY':<14}{'TYPICAL':>8}{'80% WINDOW':>15}{'±1h':>6}{'ODD':>6}"
           f"{'BY 13':>7}{'BY 14':>7}{'BY 15':>7}{'ARCHIVE=MARKET':>16}")
    print(hdr)
    print("-" * len(hdr))
    for st, s in sorted(rows_out, key=lambda x: x[1]["median_peak_min"]):
        m = s["archive_matches_settlement"]
        chk = "—" if m is None else f"{m*100:.0f}% of {s['checked_days']}"
        print(f"{st.city_label[:13]:<14}{s['median_peak']:>8}{s['p10'] + '–' + s['p90']:>15}"
              f"{s['within_1h_of_median']*100:>5.0f}%{s['odd_days']*100:>5.0f}%"
              f"{s['locked_by_13']*100:>6.0f}%{s['locked_by_14']*100:>6.0f}%{s['locked_by_15']*100:>6.0f}%{chk:>16}")
    if rows_out:
        allw = statistics.fmean(s["within_1h_of_median"] for _, s in rows_out)
        odd = statistics.fmean(s["odd_days"] for _, s in rows_out)
        print(f"\nAcross all cities: {allw*100:.0f}% of days peak within ±1 h of that city's typical time; "
              f"{odd*100:.0f}% of days peak before 10:00 or after 19:00.")
        print("TYPICAL = median time the day's high was first reached (local). ±1h = share of days within an hour\n"
              "of it. BY 13/14/15 = share of days the high was already reached by that local time.\n"
              "ARCHIVE=MARKET = how often the archive's high lands in the bucket Polymarket settled on\n"
              "(low values mean the archive misses readings for that city — trust its timing less).")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                                   "period": f"{start}..{end}", "stations": results}, indent=2))
        print(f"\nSaved → bot/calibration/{OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
