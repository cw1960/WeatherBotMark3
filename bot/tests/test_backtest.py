"""Backtest + calibration: fitting, parsing, and a full synthetic end-to-end run."""
import json
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

import backtest as bt
from weatherbot import calibration as calmod
from weatherbot.calibration import DayRecord, fit_station
from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.probability import sigma


def test_fit_recovers_bias_and_shrinks():
    rng = random.Random(1)
    recs = [DayRecord(date(2026, 7, 1) + timedelta(days=i), {"a": 20.0, "b": 21.0},
                      round(20.0 + 1.5 + rng.gauss(0, 0.7))) for i in range(60)]
    cal = fit_station(recs, "C")
    assert cal.n_days == 60
    assert 1.1 < cal.bias["a"] < 1.9           # true offset +1.5 (shrunk a little)
    assert 0.2 < cal.bias["b"] < 0.9           # true offset +0.5
    few = fit_station(recs[:3], "C")
    assert abs(few.bias["a"]) < abs(cal.bias["a"])   # few days → pulled toward 0
    assert few.sigma > cal.sigma                     # … and toward the wide default


def test_calibrated_sigma_changes_distribution_width():
    assert sigma(24, "C", sigma_full=0.8) < sigma(24, "C") < sigma(24, "C", sigma_full=2.5)
    assert sigma(0, "C", sigma_full=2.5) == sigma(0, "C")      # floor unchanged


def test_parse_settled_event_finds_winner():
    ev = {"id": 1, "slug": "s", "closed": True, "negRisk": True, "markets": [
        {"id": 1, "conditionId": "c1", "groupItemTitle": "22°C or below", "clobTokenIds": '["a","b"]', "outcomePrices": '["0","1"]'},
        {"id": 2, "conditionId": "c2", "groupItemTitle": "23°C", "clobTokenIds": '["c","d"]', "outcomePrices": '["1","0"]'},
        {"id": 3, "conditionId": "c3", "groupItemTitle": "24°C or higher", "clobTokenIds": '["e","f"]', "outcomePrices": '["0","1"]'},
    ]}
    buckets, winner = bt.parse_settled_event(ev, "C")
    assert winner == "23°C" and [b.label for b in buckets][0] == "22°C or below"
    assert bt.truth_value(next(b for b in buckets if b.label == winner)) == 23.0
    assert bt.parse_settled_event(ev, "F") is None                 # unit guard


def test_local_day_maxima_uses_station_day_and_needs_coverage():
    st = to_station(BY_SLUG["tokyo"])                                # UTC+9
    start = datetime(2026, 9, 1, 15, tzinfo=timezone.utc)           # 00:00 local on the 2nd
    times = [int((start + timedelta(hours=h)).timestamp()) for h in range(24)]
    temps = [20.0] * 24
    temps[5] = 27.0                                                  # 14:00 local
    out = bt.local_day_maxima(st, times, temps, {date(2026, 9, 2)})
    assert out == {date(2026, 9, 2): 27.0}
    assert bt.local_day_maxima(st, times[:10], temps[:10], {date(2026, 9, 2)}) == {}


# ------------------------------------------------------------------ synthetic end-to-end
async def test_backtest_end_to_end_learns_station_offset(tmp_path, monkeypatch, capsys):
    st = to_station(BY_SLUG["london"])
    tz = ZoneInfo(st.timezone)
    rng = random.Random(7)
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=2 + i) for i in range(45)]
    model_high = {d: 18.0 + 4 * ((d.toordinal() % 7) / 7) for d in days}         # varies by day
    truth = {d: round(model_high[d] + 1.5 + rng.gauss(0, 0.6)) for d in days}    # station runs +1.5

    def gamma(req):
        slug = req.url.params["slug"]
        d = next((d for d in days if bt.event_slug("london", d) == slug), None)
        if d is None:
            return httpx.Response(200, json=[])
        labels = [("14°C or below", None, 14)] + [(f"{t}°C", t, t) for t in range(15, 28)] + [("28°C or higher", 28, None)]
        markets = []
        for i, (lab, lo, hi) in enumerate(labels):
            won = (lo is None or truth[d] >= lo) and (hi is None or truth[d] <= hi)
            markets.append({"id": i, "conditionId": f"{slug}{i}", "groupItemTitle": lab,
                            "clobTokenIds": json.dumps([f"{slug}y{i}", f"{slug}n{i}"]),
                            "outcomePrices": json.dumps(["1", "0"] if won else ["0", "1"])})
        return httpx.Response(200, json=[{"id": slug, "slug": slug, "closed": True, "markets": markets}])

    def meteo(req):
        s = date.fromisoformat(req.url.params["start_date"])
        e = date.fromisoformat(req.url.params["end_date"])
        t0 = datetime.combine(s, time.min, tzinfo=timezone.utc)
        times, temps = [], []
        t = t0
        while t < datetime.combine(e + timedelta(days=1), time.min, tzinfo=timezone.utc):
            ld = t.astimezone(tz).date()
            peak = model_high.get(ld, 15.0)
            temps.append(peak if t.astimezone(tz).hour == 14 else peak - 5)
            times.append(int(t.timestamp()))
            t += timedelta(hours=1)
        return httpx.Response(200, json={"hourly": {"time": times, "temperature_2m_previous_day1": temps}})

    def router(req):
        return gamma(req) if "gamma-api" in str(req.url) else meteo(req)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(bt.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(router), **kw))
    monkeypatch.setattr(bt, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(bt, "REPORT", tmp_path / "report.txt")
    cal_path = tmp_path / "cal.json"
    monkeypatch.setattr(calmod, "CAL_PATH", cal_path)
    monkeypatch.setattr(bt, "GLOBAL_MODELS", ("ecmwf_ifs025", "icon_seamless"))
    monkeypatch.setattr(sys, "argv", ["backtest.py", "london", "--days", "45"])

    assert await bt.main() == 0
    out = capsys.readouterr().out
    saved = calmod.load(cal_path)["EGLC"]
    assert 1.1 < saved.bias["ecmwf_ifs025"] < 1.9 and saved.n_days >= 40
    # calibrated must beat generic on this station
    gen = float(out.split("Generic settings (today's bot)")[1].split("log loss=")[1].split()[0])
    cal = float(out.split("Station-calibrated")[1].split("log loss=")[1].split()[0])
    assert cal < gen
    assert (tmp_path / "report.txt").exists()


async def test_price_at_takes_last_price_before_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CACHE", tmp_path)
    hist = {"history": [{"t": 100, "p": 0.2}, {"t": 200, "p": 0.35}, {"t": 400, "p": 0.9}]}
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=hist)

    import asyncio
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await bt.price_at(http, asyncio.Semaphore(1), "tok", 300) == 0.35
        assert await bt.price_at(http, asyncio.Semaphore(1), "tok", 300) == 0.35   # cached
    assert len(calls) == 1


async def test_forecast_history_falls_back_to_past_days(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CACHE", tmp_path)
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if "start_date" in req.url.params:
            return httpx.Response(400, json={"error": True, "reason": "start_date not supported"})
        return httpx.Response(200, json={"hourly": {"time": [1, 2], "temperature_2m_previous_day1": [20.0, 21.0]}})

    import asyncio
    st = to_station(BY_SLUG["london"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        out = await bt.fetch_model_history(http, asyncio.Semaphore(1), st, "ecmwf_ifs025",
                                           date(2026, 8, 1), date(2026, 9, 1))
    assert out == ([1, 2], [20.0, 21.0]) and "past_days" in seen[-1]


def _world(n, noise, seed=3, good_err=0.3, bad_err=2.5):
    """Station truth = forecast + 1.0 + N(0, noise). Model 'good' tracks it closely,
    model 'bad' carries large random errors."""
    rng = random.Random(seed)
    recs = []
    for i in range(n):
        base = 20 + 5 * rng.random()                      # what the atmosphere "planned"
        t = round(base + 1.0 + rng.gauss(0, noise))       # station reality, unpredictable part = noise
        recs.append(DayRecord(date(2026, 6, 1) + timedelta(days=i),
                              {"good": base + rng.gauss(0, good_err),
                               "bad": base + 1.0 + rng.gauss(0, bad_err)}, t, t, t))
    return recs


def test_skilled_model_gets_more_weight():
    cal = fit_station(_world(60, 0.3), "C")
    assert cal.weights["good"] > 0.7 > cal.weights["bad"]
    assert abs(sum(cal.weights.values()) - 1) < 1e-6


def test_sigma_tracks_real_uncertainty():
    tight = fit_station(_world(80, 0.2, good_err=0.1), "C")
    loose = fit_station(_world(80, 1.8, good_err=0.1), "C")
    assert tight.sigma < 0.9 < 1.4 < loose.sigma


def test_walk_forward_probabilities_are_honest():
    """Simulated world with a known answer: the bot's stated probabilities must come true at
    about the stated rate (this is exactly the flaw the first real backtest exposed)."""
    rng = random.Random(11)
    recs = []
    for i in range(260):
        f = 20 + 6 * rng.random()
        truth = round(f + 0.8 + rng.gauss(0, 0.9))
        recs.append(DayRecord(date(2026, 1, 1) + timedelta(days=i),
                              {"a": f + rng.gauss(0, 0.2), "b": f - 0.5 + rng.gauss(0, 0.4)}, truth, truth, truth))
    buckets = [bt.Bucket("x", date.min, "e", "s", str(k), str(k), f"{k}°C", k, k, "C", "y", "n", True, True, False)
               for k in range(10, 40)]
    sc = bt.Score("t")
    for i in range(40, len(recs)):
        cal = fit_station(recs[:i], "C")
        sc.add(bt.predict(recs[i], cal, "C", buckets), f"{recs[i].truth}°C")
    for lo, hi in [(0.15, 0.3), (0.3, 0.5)]:
        sel = [(p, o) for p, o in sc.pairs if lo <= p < hi]
        said = sum(p for p, _ in sel) / len(sel)
        happened = sum(o for _, o in sel) / len(sel)
        assert abs(said - happened) < 0.08, (lo, hi, said, happened)
