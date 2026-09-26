"""
Intraday backtest — does the bot have an edge shortly BEFORE each city's daily high?

    python intraday_backtest.py                   # all active cities, last 30 days, 2 h and 1 h before peak
    python intraday_backtest.py --offsets 180 120 60
    python intraday_backtest.py nyc dallas

Requires peak_times.py to have been run (bot/calibration/peak_times.json).

For every city and each of the last N settled days, at DECISION TIME = the city's typical peak
time minus each offset (local), the bot is given only what it would really have had then:
  * airport readings up to that minute (IEM archive): high so far, latest reading
  * each model's 24-h-ahead hourly forecast (the same honest forecasts as backtest.py),
    split into the hours already past (→ how the station is running vs each model) and the
    hours still to come (→ how much more warming the models expect)
  * the station calibration learned ONLY from earlier days (walk-forward)
  * uncertainty scaled by how much of the city's usual heating window is left
It predicts every bucket, and is scored against the settled bucket, against Polymarket's price
at that exact minute, and against a blend of the two (the edge test). Paper trades use the live
bot's rules. Nothing is traded; everything is cached.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import backtest as bt
import peak_times as pk
from weatherbot.calibration import fit_station
from weatherbot.catalog import CATALOG, to_station
from weatherbot.models import Station
from weatherbot.probability import bucket_probabilities, forecast_distribution
from weatherbot.settings import settings
from weatherbot.weather.forecast import ModelDay, StationForecast, _to_unit

HERE = Path(__file__).resolve().parent
PEAKS = HERE / "calibration" / "peak_times.json"
REPORT = HERE / "calibration" / "intraday_report.txt"
MIN_OBS_BEFORE_DECISION = 3


def decision_local(day: date, peak_min: int, offset_min: int, tz: ZoneInfo) -> datetime:
    m = max(peak_min - offset_min, 0)
    return datetime.combine(day, time(m // 60, m % 60), tzinfo=tz)


def heating_hours_left(decision: datetime, p90_min: int) -> int:
    """Hours between the decision and the late end of the city's usual peak window (+1 h)."""
    now_min = decision.hour * 60 + decision.minute
    return max(0, min(12, round((p90_min + 60 - now_min) / 60)))


def obs_until(rows: list[tuple[datetime, float]], decision: datetime, unit: str):
    """(high so far, latest reading, n) from local-time archive rows before the decision."""
    naive = decision.replace(tzinfo=None)
    day0 = datetime.combine(naive.date(), time.min)
    sel = [(t, c) for t, c in rows if day0 <= t < naive]
    if len(sel) < MIN_OBS_BEFORE_DECISION:
        return None
    sel.sort()
    return (round(pk.to_unit(max(c for _, c in sel), unit), 1),
            round(pk.to_unit(sel[-1][1], unit), 1), len(sel))


def forecast_at(st: Station, hourly: dict[str, tuple[list, list]], decision: datetime,
                heat_hours: int) -> StationForecast | None:
    """Split each model's 24-h-lead hourly forecast for the local day at the decision time."""
    tz = ZoneInfo(st.timezone)
    day_start = datetime.combine(decision.date(), time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    fc = StationForecast(st.icao_code, st.unit, decision.date(), decision.astimezone(timezone.utc))
    for model, (times, temps) in hourly.items():
        past, rest, day = [], [], []
        for t, v in zip(times, temps):
            if v is None:
                continue
            ts = datetime.fromtimestamp(t, tz=timezone.utc)
            if not (day_start <= ts < day_end):
                continue
            day.append(v)
            (rest if ts >= decision else past).append(v)
        if len(day) < 20:
            continue
        fc.models[model] = ModelDay(
            model=model,
            full_day_high=_to_unit(max(day), st.unit),
            remaining_high=_to_unit(max(rest), st.unit) if rest else None,
            hours_remaining=heat_hours if rest else 0,
            elapsed_high=_to_unit(max(past), st.unit) if past else None,
            hours_elapsed=len(past),
        )
    return fc if fc.models else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--days", type=int, default=60, help="history used for walk-forward calibration")
    ap.add_argument("--test-days", type=int, default=30, help="most recent days scored against prices")
    ap.add_argument("--offsets", type=int, nargs="+", default=[120, 60],
                    help="minutes before each city's typical peak (default: 120 60)")
    args = ap.parse_args()

    if not PEAKS.exists():
        print("Run  python peak_times.py  first (it creates calibration/peak_times.json).")
        return 2
    peaks = json.loads(PEAKS.read_text())["stations"]
    entries = [e for e in CATALOG if (e.city_slug in args.cities if args.cities else e.active)]
    stations = [to_station(e) for e in entries if e.icao in peaks]
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=args.days - 1)
    days = [start + timedelta(days=i) for i in range(args.days)]
    test_from = end - timedelta(days=args.test_days - 1)
    offsets = sorted(set(args.offsets), reverse=True)
    print(f"Intraday backtest: {len(stations)} cities, test days {test_from} → {end}, "
          f"decision = typical peak − {', '.join(f'{o} min' for o in offsets)}.")

    S = {o: {"ours": bt.Score("Bot (readings + models)"), "mkt": bt.Score("Polymarket price"),
             "bl25": bt.Score("Blend: 25% ours + market"), "bl50": bt.Score("Blend: 50% ours + market"),
             "paper": [], "paper_bl": []} for o in offsets}
    per_city: dict[str, dict[int, dict]] = defaultdict(dict)
    sem = asyncio.Semaphore(4)

    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as http:
        for i, st in enumerate(stations, 1):
            pinfo = peaks[st.icao_code]
            tz = ZoneInfo(st.timezone)
            p90 = int(pinfo["p90"][:2]) * 60 + int(pinfo["p90"][3:])
            recs, hourly, _ = await bt.load_station_history(http, sem, st, days, start, end)
            iem_text = await pk.fetch_iem(http, st, start, end)
            rows = pk.parse_iem_csv(iem_text) if iem_text else []
            n_scored = 0
            for idx, (rec, buckets, winner) in enumerate(recs):
                if rec.day < test_from:
                    continue
                train = [r for r, _, _ in recs[:idx]]
                if sum(1 for r in train if r.truth is not None) < bt.MIN_TRAIN_DAYS:
                    continue
                cal = fit_station(train, st.unit)
                for off in offsets:
                    dec = decision_local(rec.day, pinfo["median_peak_min"], off, tz)
                    obs = obs_until(rows, dec, st.unit)
                    fc = forecast_at(st, hourly, dec, heating_hours_left(dec, p90))
                    if obs is None or fc is None:
                        continue
                    high, _latest, _n = obs
                    probs = bucket_probabilities(forecast_distribution(fc, high, st.unit, cal=cal), buckets)
                    ts = int(dec.timestamp())
                    prices = await asyncio.gather(*(bt.price_at(http, sem, b.token_yes, ts) for b in buckets))
                    pm = {b.label: p for b, p in zip(buckets, prices) if p is not None}
                    if len(pm) < len(buckets) - 1 or sum(pm.values()) <= 0:
                        continue
                    tot = sum(pm.values())
                    mkt = {k: v / tot for k, v in pm.items()}
                    s = S[off]
                    s["ours"].add(probs, winner); s["mkt"].add(mkt, winner)
                    s["bl25"].add(bt.blend(probs, mkt, 0.25), winner)
                    s["bl50"].add(bt.blend(probs, mkt, 0.5), winner)
                    c = per_city[st.city_label].setdefault(off, {"ours": bt.Score(""), "mkt": bt.Score(""), "bl": bt.Score("")})
                    c["ours"].add(probs, winner); c["mkt"].add(mkt, winner); c["bl"].add(bt.blend(probs, mkt, 0.5), winner)
                    if (t := bt.pick_trade(probs, pm)) is not None:
                        s["paper"].append((t[0], t[2], t[1] == winner))
                    if (t := bt.pick_trade(bt.blend(probs, mkt, 0.5), pm, min_edge=0.04)) is not None:
                        s["paper_bl"].append((t[0], t[2], t[1] == winner))
                    n_scored += 1
            print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} typical peak {pinfo['median_peak']}  "
                  f"scored decisions={n_scored}")

    lines = [f"WeatherBotMark3 intraday backtest — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"Test days {test_from} → {end}. Decision time = each city's typical peak (peak_times.py) "
             f"minus the offset, local.",
             "Bot sees: airport readings up to the decision, 24-h-ahead model forecasts, calibration from earlier days.",
             "LOWER log loss = better.", ""]
    for off in offsets:
        s = S[off]
        lines.append(f"=== {off} minutes before typical peak ===")
        if not s["mkt"].logloss:
            lines.append("  (no scored decisions)")
            continue
        lines += ["  " + s["ours"].line(), "  " + s["mkt"].line(), "  " + s["bl25"].line(), "  " + s["bl50"].line()]
        m = statistics.fmean(s["mkt"].logloss)
        b = min(statistics.fmean(s["bl25"].logloss), statistics.fmean(s["bl50"].logloss))
        lines.append("  Edge test: " + ("YES — adding the bot's view improves on the market's price."
                                        if b < m - 0.005 else "NO — the market's price already contains what the bot knows."))
        lines += bt.paper_lines(f"  Paper trades — live bot rules", s["paper"])
        lines += bt.paper_lines(f"  Paper trades — blend rules", s["paper_bl"])
        lines += ["", "  Reliability — bot:"] + s["ours"].reliability()
        lines += ["  Reliability — market:"] + s["mkt"].reliability() + [""]

    near = min(offsets)
    lines += [f"Per city at {near} min before peak (ours / market / blend; ✓ = blend beats market):",
              f"{'City':<14}{'n':>4}{'ours':>8}{'market':>8}{'blend':>8}"]
    rows_out = []
    for name, by_off in per_city.items():
        c = by_off.get(near)
        if c and c["mkt"].logloss:
            o, m, bl = (statistics.fmean(c[k].logloss) for k in ("ours", "mkt", "bl"))
            rows_out.append((bl - m, name, len(c["mkt"].logloss), o, m, bl))
    for d, name, n, o, m, bl in sorted(rows_out):
        lines.append(f"{name:<14}{n:>4}{o:>8.3f}{m:>8.3f}{bl:>8.3f}  {'✓' if d < -0.005 else ''}")
    lines += ["", "Caveats: prices are last TRADED prices (the ask is higher); archive readings are hourly for",
              "many stations (can miss a half-hourly peak); forecasts are 24 h old (the live bot has fresher ones)."]

    report = "\n".join(lines)
    print("\n" + report)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report + "\n")
    print(f"\nReport saved → bot/calibration/{REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
