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

Intraday calibration (v2): the first run showed the bot was overconfident at these times (said
77% → happened 60%). So each city also learns, walk-forward, its own uncertainty AT the decision
time (spread of further heating + how far the settled high can sit above the reports seen),
pulled toward the all-city value when it has few days. The report shows the bot before and after.
The final per-city values are saved to bot/calibration/intraday_calibration.json, which the live
bot uses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from math import log
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import backtest as bt
import peak_times as pk
from weatherbot.calibration import (IntradayCal, StationCal, fit_station, intraday_grid,
                                     pick_intraday, save_intraday)
from weatherbot.catalog import CATALOG, to_station
from weatherbot.models import Station
from weatherbot.probability import bucket_probabilities, distribution, forecast_distribution, forecast_means
from weatherbot.settings import settings
from weatherbot.weather.forecast import ModelDay, StationForecast, _to_unit

HERE = Path(__file__).resolve().parent
PEAKS = HERE / "calibration" / "peak_times.json"
REPORT = HERE / "calibration" / "intraday_report.txt"
OUT_CAL = HERE / "calibration" / "intraday_calibration.json"
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


@dataclass
class Case:
    """One decision the bot would have made (one city, one day, one offset)."""
    day: date
    decision: datetime
    fc: StationForecast
    high: float
    buckets: list
    winner: str
    cal: StationCal
    losses: list[float]          # log loss under every intraday_grid pair


def case_losses(fc: StationForecast, high: float, unit: str, cal: StationCal | None,
                buckets: list, winner: str) -> list[float]:
    means, weights, hrs = forecast_means(fc, high, unit, cal)
    wb = [b for b in buckets if b.label == winner]
    out = []
    for s, g in intraday_grid(unit):
        dist = distribution(observed_high=high, means=means, hours_remaining=hrs, unit=unit,
                            weights=weights, sigma_fixed=s, obs_gap=g)
        out.append(-log(max(bucket_probabilities(dist, wb).get(winner, 0.0), 1e-4)))
    return out


class Pooled:
    """All-city mean loss per grid pair using only decisions from days BEFORE a given day."""
    def __init__(self, cases: list[Case]) -> None:
        by_day: dict[date, list[list[float]]] = defaultdict(list)
        for c in cases:
            by_day[c.day].append(c.losses)
        self.days = sorted(by_day)
        self.cum: dict[date, tuple[list[float], int]] = {}
        acc: list[float] | None = None
        n = 0
        for d in self.days:
            self.cum[d] = (list(acc) if acc else [], n)
            for row in by_day[d]:
                acc = list(row) if acc is None else [a + b for a, b in zip(acc, row)]
                n += 1
        self.total = (acc or [], n)

    def before(self, d: date) -> list[float] | None:
        acc, n = next((self.cum[x] for x in self.days if x >= d), self.total)
        return [a / n for a in acc] if n else None

    def all(self) -> list[float] | None:
        acc, n = self.total
        return [a / n for a in acc] if n else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--days", type=int, default=60, help="history used for walk-forward calibration")
    ap.add_argument("--test-days", type=int, default=30, help="most recent days scored against prices")
    ap.add_argument("--offsets", type=int, nargs="+", default=[120, 60],
                    help="minutes before each city's typical peak (default: 120 60)")
    ap.add_argument("--no-save", action="store_true", help="don't write intraday_calibration.json")
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

    # ---- phase 1: every decision the bot could have made (all days, for walk-forward learning)
    cases: dict[int, dict[str, list[Case]]] = {o: defaultdict(list) for o in offsets}
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as http:
        for i, st in enumerate(stations, 1):
            pinfo = peaks[st.icao_code]
            tz = ZoneInfo(st.timezone)
            p90 = int(pinfo["p90"][:2]) * 60 + int(pinfo["p90"][3:])
            recs, hourly, _ = await bt.load_station_history(http, sem, st, days, start, end)
            iem_text = await pk.fetch_iem(http, st, start, end)
            rows = pk.parse_iem_csv(iem_text) if iem_text else []
            n = 0
            for idx, (rec, buckets, winner) in enumerate(recs):
                if winner is None:
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
                    high = obs[0]
                    cases[off][st.icao_code].append(Case(
                        rec.day, dec, fc, high, buckets, winner, cal,
                        case_losses(fc, high, st.unit, cal, buckets, winner)))
                    n += 1
            print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} typical peak {pinfo['median_peak']}  "
                  f"decisions={n}")

        # ---- phase 2: score the test days against prices (intraday values from EARLIER days only)
        S = {o: {"old": bt.Score("Bot before (generic uncertainty)"),
                 "ours": bt.Score("Bot, calibrated intraday"), "mkt": bt.Score("Polymarket price"),
                 "bl25": bt.Score("Blend: 25% ours + market"), "bl50": bt.Score("Blend: 50% ours + market"),
                 "paper": [], "paper_bl25": [], "paper_bl50": []} for o in offsets}
        per_city: dict[str, dict[int, dict]] = defaultdict(dict)
        pooled = {o: Pooled([c for cs in cases[o].values() for c in cs]) for o in offsets}
        print("Scoring test days against Polymarket prices…")
        for off in offsets:
            for st in stations:
                cs = cases[off].get(st.icao_code, [])
                for case in cs:
                    if case.day < test_from:
                        continue
                    prior = [c.losses for c in cs if c.day < case.day]
                    ical = pick_intraday(prior, pooled[off].before(case.day), st.unit)
                    old = bucket_probabilities(forecast_distribution(case.fc, case.high, st.unit, cal=case.cal),
                                               case.buckets)
                    probs = bucket_probabilities(forecast_distribution(case.fc, case.high, st.unit, cal=case.cal,
                                                                       intraday=ical), case.buckets)
                    ts = int(case.decision.timestamp())
                    prices = await asyncio.gather(*(bt.price_at(http, sem, b.token_yes, ts) for b in case.buckets))
                    pm = {b.label: p for b, p in zip(case.buckets, prices) if p is not None}
                    if len(pm) < len(case.buckets) - 1 or sum(pm.values()) <= 0:
                        continue
                    tot = sum(pm.values())
                    mkt = {k: v / tot for k, v in pm.items()}
                    w = case.winner
                    s = S[off]
                    b25, b50 = bt.blend(probs, mkt, 0.25), bt.blend(probs, mkt, 0.5)
                    s["old"].add(old, w); s["ours"].add(probs, w); s["mkt"].add(mkt, w)
                    s["bl25"].add(b25, w); s["bl50"].add(b50, w)
                    c = per_city[st.city_label].setdefault(off, {
                        "ours": bt.Score(""), "mkt": bt.Score(""), "bl25": bt.Score(""), "bl50": bt.Score(""),
                        "ical": None})
                    c["ours"].add(probs, w); c["mkt"].add(mkt, w); c["bl25"].add(b25, w); c["bl50"].add(b50, w)
                    c["ical"] = ical
                    if (t := bt.pick_trade(probs, pm)) is not None:
                        s["paper"].append((t[0], t[2], t[1] == w))
                    for key, bl in (("paper_bl25", b25), ("paper_bl50", b50)):
                        if (t := bt.pick_trade(bl, pm, min_edge=0.04)) is not None:
                            s[key].append((t[0], t[2], t[1] == w))

    # ---- final per-city values for the live bot: learned from ALL decisions
    final: dict[int, dict[str, IntradayCal]] = {}
    pooled_c: dict[int, IntradayCal] = {}
    for off in offsets:
        allm = pooled[off].all()
        final[off] = {}
        if allm:
            pooled_c[off] = pick_intraday([], allm, "C")
        for st in stations:
            cs = cases[off].get(st.icao_code, [])
            if cs and (ic := pick_intraday([c.losses for c in cs], allm, st.unit)) is not None:
                final[off][st.icao_code] = ic

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
        lines += ["  " + s[k].line() for k in ("old", "ours", "mkt", "bl25", "bl50")]
        m = statistics.fmean(s["mkt"].logloss)
        b = min(statistics.fmean(s["bl25"].logloss), statistics.fmean(s["bl50"].logloss))
        lines.append("  Edge test: " + ("YES — adding the bot's view improves on the market's price."
                                        if b < m - 0.005 else "NO — the market's price already contains what the bot knows."))
        if off in pooled_c:
            pc = pooled_c[off]
            lines.append(f"  All-city intraday uncertainty: spread {pc.sigma:.1f}°C, "
                         f"settled-above-reports allowance {pc.obs_gap:.1f}°C")
        lines += bt.paper_lines("  Paper trades — old live rules (bot alone, edge ≥ 8%)", s["paper"])
        lines += bt.paper_lines("  Paper trades — blend 25% ours, edge ≥ 4%", s["paper_bl25"])
        lines += bt.paper_lines("  Paper trades — blend 50% ours, edge ≥ 4%", s["paper_bl50"])
        lines += ["", "  Reliability — bot before:"] + s["old"].reliability()
        lines += ["  Reliability — bot calibrated:"] + s["ours"].reliability()
        lines += ["  Reliability — market:"] + s["mkt"].reliability() + [""]

    near = min(offsets)
    lines += [f"Per city at {near} min before peak (✓ = a blend beats the market; spread/allow = learned values):",
              f"{'City':<14}{'n':>4}{'ours':>8}{'market':>8}{'bl25':>8}{'bl50':>8}{'spread':>8}{'allow':>7}"]
    rows_out = []
    for name, by_off in per_city.items():
        c = by_off.get(near)
        if c and c["mkt"].logloss:
            o, m, b25, b50 = (statistics.fmean(c[k].logloss) for k in ("ours", "mkt", "bl25", "bl50"))
            rows_out.append((min(b25, b50) - m, name, len(c["mkt"].logloss), o, m, b25, b50, c["ical"]))
    for d, name, n, o, m, b25, b50, ic in sorted(rows_out, key=lambda r: r[0]):
        lines.append(f"{name:<14}{n:>4}{o:>8.3f}{m:>8.3f}{b25:>8.3f}{b50:>8.3f}"
                     f"{ic.sigma if ic else 0:>8.2f}{ic.obs_gap if ic else 0:>7.2f}  {'✓' if d < -0.005 else ''}")
    lines += ["", "Caveats: prices are last TRADED prices (the ask is higher); archive readings are hourly for",
              "many stations (can miss a half-hourly peak); forecasts are 24 h old (the live bot has fresher ones)."]

    report = "\n".join(lines)
    print("\n" + report)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report + "\n")
    print(f"\nReport saved → bot/calibration/{REPORT.name}")
    if not args.no_save and any(final.values()):
        save_intraday(final, pooled_c, OUT_CAL,
                      meta={"generated": datetime.now(timezone.utc).isoformat(), "period": f"{start}..{end}",
                            "offsets_min_before_peak": offsets})
        print(f"Intraday calibration saved → bot/calibration/{OUT_CAL.name} (used by the live bot)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
