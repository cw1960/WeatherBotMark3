"""
Backtest on history — are the bot's probabilities honest?

    python backtest.py                      # last 60 days, all active cities
    python backtest.py --days 90            # longer history (slower first run)
    python backtest.py nyc london           # just these cities
    python backtest.py --prices             # also compare with Polymarket's prices at the time
                                            #   (many more downloads — try with a few cities first)

What it does, per city and per past day:
  1. TRUTH: the bucket the Polymarket market actually settled on (Gamma API, closed events).
     We use the market's own result, not a weather archive — archives can miss the peak
     (London City 1 Sep 2026: hourly archive says 22°C, market settled 23°C).
  2. HONEST FORECAST: each model's hourly forecast made ~24 h ahead (Open-Meteo Previous Runs
     API, `temperature_2m_previous_day1`), turned into that model's forecast daily high for the
     station's local day. No hindsight.
  3. WALK-FORWARD: for each test day, learn the station's model offsets and spread ONLY from
     earlier days, predict the bucket probabilities as the bot would at local midnight, score.
  4. REPORT: log loss + a reliability table ("when we said 30%, did it happen ~30%?") for
     (a) the current generic settings and (b) the station-calibrated settings; optionally the
     market's own prices and a paper-trading tally.
  5. SAVE: calibration learned from ALL days → bot/calibration/station_calibration.json,
     which the live bot and reports load automatically.

Public data only; nothing is traded. Downloads are cached in bot/.backtest_cache/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from weatherbot import calibration as calmod
from weatherbot.calibration import DayRecord, StationCal, fit_station
from weatherbot.catalog import CATALOG, to_station
from weatherbot.exchange.markets import Bucket, _json_list, event_slug, parse_bucket
from weatherbot.models import Station
from weatherbot.probability import bucket_probabilities, distribution
from weatherbot.settings import settings
from weatherbot.weather.forecast import GLOBAL_MODELS, US_ONLY_MODELS

CACHE = Path(__file__).resolve().parent / ".backtest_cache"
REPORT = Path(__file__).resolve().parent / "calibration" / "backtest_report.txt"
GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
MIN_TRAIN_DAYS = 14
EDGE_FOR_PAPER_TRADES = 0.08
PAPER_STAKE = 10.0


# ----------------------------------------------------------------------------- cache
def _cache_get(kind: str, key: str):
    p = CACHE / kind / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text())
    return None


def _cache_put(kind: str, key: str, value) -> None:
    p = CACHE / kind / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value))


async def _get_json(http: httpx.AsyncClient, url: str, params: dict, tries: int = 4):
    for attempt in range(tries):
        try:
            r = await http.get(url, params=params, timeout=60)
        except httpx.TransportError:
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        return r
    return None


# ----------------------------------------------------------------------------- data: truth
def parse_settled_event(ev: dict, unit: str) -> tuple[list[Bucket], str | None] | None:
    """Buckets + the winning label from a closed Gamma event, or None if not cleanly settled."""
    buckets, winner = [], None
    for m in ev.get("markets", []):
        try:
            lo, hi, u = parse_bucket(m.get("groupItemTitle") or "")
        except ValueError:
            return None
        if u != unit:
            return None
        tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        prices = [str(p) for p in _json_list(m.get("outcomePrices"))]
        if len(tokens) != 2:
            return None
        label = m["groupItemTitle"]
        buckets.append(Bucket("", date.min, str(ev.get("id")), ev.get("slug", ""), str(m.get("id")),
                              str(m.get("conditionId")), label, lo, hi, u, tokens[0], tokens[1],
                              bool(ev.get("negRisk")), True, True))
        if prices and prices[0] in ("1", "1.0"):
            winner = label
    if not buckets or winner is None:
        return None
    buckets.sort(key=lambda b: (b.lower is not None, b.lower if b.lower is not None else 0))
    return buckets, winner


def truth_value(bucket: Bucket) -> float | None:
    """Numeric settled high for bias fitting (None for open-ended buckets)."""
    if bucket.lower is None or bucket.upper is None:
        return None
    return (bucket.lower + bucket.upper) / 2


async def fetch_settled(http, sem, st: Station, day: date):
    slug = event_slug(st.city_slug, day)
    cached = _cache_get("gamma", slug)
    if cached is None:
        async with sem:
            r = await _get_json(http, GAMMA, {"slug": slug})
        if r is None or r.status_code != 200:
            return None
        cached = r.json()
        if cached and cached[0].get("closed"):
            _cache_put("gamma", slug, cached)   # only cache settled events
    if not cached:
        return None
    return parse_settled_event(cached[0], st.unit)


# ----------------------------------------------------------------------------- data: forecasts
def local_day_maxima(st: Station, times: list[int], temps: list, days: set[date]) -> dict[date, float]:
    """Model's max over each local day (needs ≥ 20 of 24 hours), converted to station unit."""
    tz = ZoneInfo(st.timezone)
    per_day: dict[date, list[float]] = defaultdict(list)
    for t, v in zip(times, temps):
        if v is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(tz).date()
        if d in days:
            per_day[d].append(v)
    out = {}
    for d, vals in per_day.items():
        if len(vals) >= 20:
            c = max(vals)
            out[d] = round(c * 9 / 5 + 32, 1) if st.unit == "F" else round(c, 1)
    return out


async def fetch_model_history(http, sem, st: Station, model: str, start: date, end: date):
    key = f"{st.icao_code}_{model}_{start}_{end}"
    cached = _cache_get("previous_runs", key)
    if cached is None:
        base = ("https://customer-previous-runs-api.open-meteo.com/v1/forecast"
                if settings.open_meteo_api_key else "https://previous-runs-api.open-meteo.com/v1/forecast")
        params = {
            "latitude": st.latitude, "longitude": st.longitude,
            "hourly": "temperature_2m_previous_day1", "models": model,
            "start_date": (start - timedelta(days=1)).isoformat(),
            "end_date": (end + timedelta(days=1)).isoformat(),
            "timeformat": "unixtime", "temperature_unit": "celsius",
        }
        if settings.open_meteo_api_key:
            params["apikey"] = settings.open_meteo_api_key
        async with sem:
            r = await _get_json(http, base, params)
            if r is not None and r.status_code == 400:
                # Fallback if this API doesn't take start/end dates: ask for the last N days.
                alt = {k: v for k, v in params.items() if k not in ("start_date", "end_date")}
                alt.update(past_days=min(92, (datetime.now(timezone.utc).date() - start).days + 2),
                           forecast_days=1)
                r = await _get_json(http, base, alt)
        if r is None or r.status_code != 200:
            reason = "" if r is None else r.text[:160]
            print(f"      ! {st.icao_code} {model}: no forecast history ({reason})")
            return None
        cached = r.json()
        _cache_put("previous_runs", key, cached)
    hourly = cached.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m_previous_day1")
    if temps is None:  # multi-model style key, just in case
        temps = next((v for k, v in hourly.items() if k.startswith("temperature_2m_previous_day1")), None)
    return (times, temps) if temps else None


# ----------------------------------------------------------------------------- data: prices
async def price_at(http, sem, token: str, ts: int) -> float | None:
    key = f"{token}_{ts}"
    cached = _cache_get("prices", key)
    if cached is None:
        async with sem:
            r = await _get_json(http, CLOB_HISTORY, {"market": token, "startTs": ts - 6 * 3600,
                                                     "endTs": ts, "fidelity": 60})
        if r is None or r.status_code != 200:
            return None
        cached = r.json().get("history", [])
        _cache_put("prices", key, cached)
    pts = [p for p in cached if p.get("t", 0) <= ts]
    return float(pts[-1]["p"]) if pts else None


# ----------------------------------------------------------------------------- scoring
def predict(rec: DayRecord, cal: StationCal | None, unit: str, buckets: list[Bucket]) -> dict[str, float]:
    """Bot's probabilities at local midnight: no observations yet, a full day of heating ahead."""
    bias = cal.bias if cal else {}
    means = [v + bias.get(m, 0.0) for m, v in rec.model_max.items()]
    dist = distribution(observed_high=None, means=means, hours_remaining=24, unit=unit,
                        sigma_full=cal.sigma if cal else None)
    return bucket_probabilities(dist, buckets)


class Score:
    def __init__(self, name: str) -> None:
        self.name, self.logloss, self.brier, self.pairs = name, [], [], []

    def add(self, probs: dict[str, float], winner: str) -> None:
        total = sum(probs.values()) or 1.0
        probs = {k: v / total for k, v in probs.items()}
        self.logloss.append(-math.log(max(probs.get(winner, 0.0), 1e-4)))
        self.brier.append(sum((p - (1.0 if k == winner else 0.0)) ** 2 for k, p in probs.items()))
        self.pairs += [(p, 1.0 if k == winner else 0.0) for k, p in probs.items()]

    def line(self) -> str:
        if not self.logloss:
            return f"{self.name:<34} (no data)"
        return (f"{self.name:<34} days={len(self.logloss):>4}  log loss={statistics.fmean(self.logloss):.3f}"
                f"  Brier={statistics.fmean(self.brier):.3f}")

    def reliability(self) -> list[str]:
        bins = [(0, .05), (.05, .15), (.15, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]
        rows = [f"    {'we said':>12}  {'happened':>9}  {'count':>6}"]
        for lo, hi in bins:
            sel = [(p, o) for p, o in self.pairs if lo <= p < hi]
            if sel:
                rows.append(f"    {f'{lo*100:.0f}–{min(hi,1)*100:.0f}%':>12}  "
                            f"{statistics.fmean(o for _, o in sel)*100:>8.1f}%  {len(sel):>6}"
                            f"   (avg said {statistics.fmean(p for p, _ in sel)*100:.1f}%)")
        return rows


# ----------------------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--prices", action="store_true", help="compare with Polymarket prices at local midnight")
    ap.add_argument("--no-save", action="store_true", help="don't write the calibration file")
    args = ap.parse_args()

    entries = [e for e in CATALOG if (e.city_slug in args.cities if args.cities else e.active)]
    if not entries:
        print("No matching cities.")
        return 2
    stations = [to_station(e) for e in entries]
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=2)                  # settled for sure
    start = end - timedelta(days=args.days - 1)
    days = [start + timedelta(days=i) for i in range(args.days)]
    print(f"Backtest {start} → {end} ({args.days} days) for {len(stations)} cities.")
    print("First run downloads a lot (cached afterwards). Progress:")

    records: dict[str, list[tuple[DayRecord, list[Bucket], str]]] = defaultdict(list)
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as http:
        for i, st in enumerate(stations, 1):
            settled = await asyncio.gather(*(fetch_settled(http, sem, st, d) for d in days))
            have = {d: s for d, s in zip(days, settled) if s}
            models = US_ONLY_MODELS + GLOBAL_MODELS if st.country == "US" else GLOBAL_MODELS
            hist = await asyncio.gather(*(fetch_model_history(http, sem, st, m, start, end) for m in models))
            maxima: dict[str, dict[date, float]] = {}
            for m, h in zip(models, hist):
                if h:
                    maxima[m] = local_day_maxima(st, h[0], h[1], set(have))
            for d, (buckets, winner) in sorted(have.items()):
                mm = {m: v[d] for m, v in maxima.items() if d in v}
                if not mm:
                    continue
                wb = next(b for b in buckets if b.label == winner)
                records[st.city_slug].append((DayRecord(d, mm, truth_value(wb)), buckets, winner))
            print(f"  [{i:>2}/{len(stations)}] {st.city_label:<14} settled days={len(have):>3}  "
                  f"models with history={len(maxima)}  usable days={len(records[st.city_slug])}")

        # ------------------------------------------------ walk-forward scoring
        generic, calibrated, market = Score("Generic settings (today's bot)"), Score("Station-calibrated"), Score("Polymarket price")
        per_city = []
        paper = []  # (edge, price, won)
        price_jobs = []
        for st in stations:
            recs = records[st.city_slug]
            g_city, c_city = Score(""), Score("")
            for idx, (rec, buckets, winner) in enumerate(recs):
                train = [r for r, _, _ in recs[:idx]]
                if sum(1 for r in train if r.truth is not None) < MIN_TRAIN_DAYS:
                    continue
                cal = fit_station(train, st.unit)
                pg = predict(rec, None, st.unit, buckets)
                pc = predict(rec, cal, st.unit, buckets)
                generic.add(pg, winner); calibrated.add(pc, winner)
                g_city.add(pg, winner); c_city.add(pc, winner)
                if args.prices:
                    ts = int(datetime.combine(rec.day, time.min, tzinfo=ZoneInfo(st.timezone)).timestamp())
                    price_jobs.append((st, rec, buckets, winner, pc, ts))
            if g_city.logloss:
                per_city.append((st.city_label, len(g_city.logloss),
                                 statistics.fmean(g_city.logloss), statistics.fmean(c_city.logloss)))

        if args.prices and price_jobs:
            print(f"Fetching market prices for {len(price_jobs)} city-days …")
            for st, rec, buckets, winner, pc, ts in price_jobs:
                prices = await asyncio.gather(*(price_at(http, sem, b.token_yes, ts) for b in buckets))
                pm = {b.label: p for b, p in zip(buckets, prices) if p is not None}
                if len(pm) >= len(buckets) - 1 and sum(pm.values()) > 0:
                    market.add(pm, winner)
                    for b in buckets:
                        pr = pm.get(b.label)
                        if pr and 0.01 <= pr <= 0.95 and pc[b.label] - pr >= EDGE_FOR_PAPER_TRADES:
                            paper.append((pc[b.label] - pr, pr, b.label == winner))

    # ------------------------------------------------ fit on everything & save
    final = {st.icao_code: fit_station([r for r, _, _ in records[st.city_slug]], st.unit) for st in stations}
    final = {k: v for k, v in final.items() if v.n_days > 0}

    lines = [f"WeatherBotMark3 backtest — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"Period {start} → {end}; decision time = local midnight; forecasts = 24 h lead.",
             f"Walk-forward: each day scored with settings learned only from earlier days "
             f"(needs ≥ {MIN_TRAIN_DAYS} earlier days).", "",
             "LOWER log loss = better. Guessing evenly across ~11 buckets scores about 2.40.", "",
             generic.line(), calibrated.line()]
    if market.logloss:
        lines.append(market.line())
    lines += ["", "Reliability — station-calibrated (want 'happened' ≈ 'we said'):"] + calibrated.reliability()
    lines += ["", "Reliability — generic settings:"] + generic.reliability()
    if market.logloss:
        lines += ["", "Reliability — Polymarket prices:"] + market.reliability()
    if paper:
        pnl = sum((PAPER_STAKE / pr - PAPER_STAKE) if won else -PAPER_STAKE for _, pr, won in paper)
        lines += ["", f"Paper trades (edge ≥ {EDGE_FOR_PAPER_TRADES:.0%} at the market price, ${PAPER_STAKE:.0f} each):",
                  f"    trades={len(paper)}  won={sum(w for *_, w in paper)}  "
                  f"P&L=${pnl:+.2f}  ROI={pnl / (PAPER_STAKE * len(paper)):+.1%}",
                  "    (Optimistic: uses traded prices, not the ask you'd actually pay.)"]
    lines += ["", f"{'City':<14}{'days':>6}{'generic':>10}{'calibrated':>12}"]
    for name, n, g, c in sorted(per_city, key=lambda x: x[3] - x[2]):
        lines.append(f"{name:<14}{n:>6}{g:>10.3f}{c:>12.3f}")
    lines += ["", "Learned station settings (all days):",
              f"{'ICAO':<6}{'days':>5}{'sigma':>8}  model offsets (station − model)"]
    for icao, c in sorted(final.items()):
        offs = ", ".join(f"{m.replace('_seamless','')}:{b:+.1f}" for m, b in sorted(c.bias.items()))
        lines.append(f"{icao:<6}{c.n_days:>5}{c.sigma:>8.2f}  {offs}")

    report = "\n".join(lines)
    print("\n" + report)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report + "\n")
    if not args.no_save and final:
        existing = calmod.load()
        existing.update(final)
        calmod.save(existing, meta={"generated": datetime.now(timezone.utc).isoformat(),
                                    "period": f"{start}..{end}", "lead": "previous_day1"})
        print(f"\nSaved calibration for {len(final)} stations → bot/calibration/{calmod.CAL_PATH.name}")
    print(f"Report saved → bot/calibration/{REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
