"""Intraday backtest: decision times, no look-ahead, forecast splitting, full synthetic run."""
import json
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import intraday_backtest as ib
import backtest as bt
from weatherbot.calibration import DayRecord
from weatherbot.catalog import BY_SLUG, to_station

LON = to_station(BY_SLUG["london"])
TZ = ZoneInfo(LON.timezone)


def test_decision_and_heating_window():
    d = ib.decision_local(date(2026, 9, 1), 14 * 60 + 50, 120, TZ)
    assert d == datetime(2026, 9, 1, 12, 50, tzinfo=TZ)
    assert ib.heating_hours_left(d, 16 * 60 + 20) == 4          # until 17:20
    assert ib.heating_hours_left(datetime(2026, 9, 1, 18, 0, tzinfo=TZ), 16 * 60 + 20) == 0


def test_obs_until_never_looks_ahead():
    rows = [(datetime(2026, 9, 1, h, 50), 15.0 + h * 0.5) for h in range(24)]
    rows.append((datetime(2026, 8, 31, 23, 50), 40.0))             # yesterday — ignored
    high, latest, n = ib.obs_until(rows, datetime(2026, 9, 1, 13, 0, tzinfo=TZ), "C")
    assert n == 13 and high == 21.0 and latest == 21.0             # up to 12:50 only
    assert ib.obs_until(rows, datetime(2026, 9, 1, 1, 0, tzinfo=TZ), "C") is None   # too few


def test_forecast_split_at_decision():
    day = date(2026, 9, 1)
    t0 = datetime.combine(day, time.min, tzinfo=TZ).astimezone(timezone.utc)
    times = [int((t0 + timedelta(hours=h)).timestamp()) for h in range(24)]
    temps = [15.0 + (6 if h == 15 else 0) + (3 if h == 9 else 0) for h in range(24)]
    fc = ib.forecast_at(LON, {"ecmwf_ifs025": (times, temps)}, datetime(2026, 9, 1, 12, 0, tzinfo=TZ), 5)
    m = fc.models["ecmwf_ifs025"]
    assert m.full_day_high == 21.0 and m.remaining_high == 21.0 and m.elapsed_high == 18.0
    assert m.hours_elapsed == 12 and m.hours_remaining == 5


async def _world(tmp_path, monkeypatch, price_fn):
    """London: typical peak 15:00; models forecast the day; station ends at forecast+1.5±0.5.
    By 14:00 the readings already show most of the day's warmth."""
    rng = random.Random(5)
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    days = [end - timedelta(days=i) for i in range(45)][::-1]
    fmax = {d: 18 + (d.toordinal() % 5) for d in days}
    truth = {d: round(fmax[d] + 1.5 + rng.gauss(0, 0.5)) for d in days}
    labels = [("14°C or below", None, 14)] + [(f"{t}°C", t, t) for t in range(15, 28)] + [("28°C or higher", 28, None)]

    def buckets():
        return [bt.Bucket("london", date.min, "e", "s", str(i), str(i), lab, lo, hi, "C", f"tok{i}", "n", True, True, False)
                for i, (lab, lo, hi) in enumerate(labels)]

    async def fake_history(http, sem, st, days_, start, end_):
        recs = [(DayRecord(d, {"ecmwf_ifs025": fmax[d]}, truth[d], truth[d], truth[d]), buckets(), f"{truth[d]}°C")
                for d in days]
        times, temps = [], []
        for d in days:
            t0 = datetime.combine(d, time.min, tzinfo=TZ).astimezone(timezone.utc)
            for h in range(24):
                times.append(int((t0 + timedelta(hours=h)).timestamp()))
                temps.append(fmax[d] if h == 15 else fmax[d] - 4)
        return recs, {"ecmwf_ifs025": (times, temps)}, {}

    async def fake_iem(http, st, start, end_):
        lines = ["station,valid,tmpc"]
        for d in days:
            for h in range(24):
                v = truth[d] if h >= 14 else truth[d] - 3     # the high is already in by 14:50
                lines.append(f"EGLC,{d} {h:02d}:50,{v}")
        return "\n".join(lines)

    async def fake_price(http, sem, token, ts):
        d = datetime.fromtimestamp(ts, tz=TZ).date()
        i = int(token[3:])
        return price_fn(labels[i][0], f"{truth[d]}°C")

    peaks = tmp_path / "peaks.json"
    peaks.write_text(json.dumps({"stations": {"EGLC": {"median_peak": "15:00", "median_peak_min": 900,
                                                       "p10": "13:00", "p90": "16:00"}}}))
    monkeypatch.setattr(ib, "PEAKS", peaks)
    monkeypatch.setattr(ib, "REPORT", tmp_path / "r.txt")
    monkeypatch.setattr(ib, "OUT_CAL", tmp_path / "intraday.json")
    monkeypatch.setattr(bt, "load_station_history", fake_history)
    monkeypatch.setattr(ib.pk, "fetch_iem", fake_iem)
    monkeypatch.setattr(bt, "price_at", fake_price)
    monkeypatch.setattr(sys, "argv", ["intraday_backtest.py", "london", "--days", "45", "--test-days", "25",
                                      "--offsets", "120", "60"])
    assert await ib.main() == 0
    return (tmp_path / "r.txt").read_text()


async def test_edge_found_when_market_ignores_readings(tmp_path, monkeypatch):
    report = await _world(tmp_path, monkeypatch, lambda lab, win: 1 / 15)
    sixty = report.split("=== 60 minutes")[1]
    assert "Edge test: YES" in sixty
    ours = float(sixty.split("Bot, calibrated intraday")[1].split("log loss=")[1].split()[0])
    assert ours < 1.0                                          # readings make the bot sharp
    assert "Bot, calibrated intraday" in sixty and "Bot before" in sixty
    from weatherbot.calibration import load_intraday
    stations, default = load_intraday(60, tmp_path / "intraday.json")
    assert "EGLC" in stations and default is not None and stations["EGLC"].n_days > 20


async def test_no_edge_when_market_already_knows(tmp_path, monkeypatch):
    report = await _world(tmp_path, monkeypatch, lambda lab, win: 0.97 if lab == win else 0.002)
    assert "Edge test: NO" in report.split("=== 60 minutes")[1]


# ------------------------------------------------------------- intraday calibration pieces
from weatherbot import calibration as calm


def test_pick_intraday_learns_and_shrinks_toward_pooled():
    grid = calm.intraday_grid("C")
    j_city, j_pool = 5, 40
    city = [[0.5 if j == j_city else 2.0 for j in range(len(grid))] for _ in range(30)]
    pooled = [0.5 if j == j_pool else 1.0 for j in range(len(grid))]
    assert (calm.pick_intraday(city, pooled, "C").sigma, calm.pick_intraday(city, pooled, "C").obs_gap) == grid[j_city]
    few = city[:1]                                            # 1 day: pooled choice wins
    ic = calm.pick_intraday(few, pooled, "C")
    assert (ic.sigma, ic.obs_gap) == grid[j_pool] and ic.n_days == 1
    assert calm.pick_intraday([], None, "C") is None


def test_intraday_grid_scales_for_fahrenheit():
    assert calm.intraday_grid("F")[0] == (round(0.3 * 1.8, 3), round(0.2 * 1.8, 3))
    assert len(calm.intraday_grid("F")) == len(calm.intraday_grid("C"))


def test_intraday_save_load_and_fallback(tmp_path):
    p = tmp_path / "i.json"
    calm.save_intraday({120: {"EGLC": calm.IntradayCal(1.2, 0.4, 20)}, 60: {"EGLC": calm.IntradayCal(0.8, 0.4, 20)}},
                       {60: calm.IntradayCal(1.0, 0.7), 120: calm.IntradayCal(1.4, 0.7)}, p)
    st, d = calm.load_intraday(70, p)                         # nearest saved offset = 60
    assert st["EGLC"].sigma == 0.8 and d.sigma == 1.0
    nyc = calm.intraday_for("KLGA", "F", st, d)               # no own values → pooled, in °F
    assert nyc.sigma == 1.8 and nyc.obs_gap == round(0.7 * 1.8, 3)
    assert calm.load_intraday(60, tmp_path / "missing.json") == ({}, None)


def test_intraday_values_change_the_distribution():
    from weatherbot.probability import distribution
    tight = distribution(observed_high=20.0, means=[20.5], hours_remaining=2, unit="C", sigma_fixed=0.3, obs_gap=0.2)
    wide = distribution(observed_high=20.0, means=[20.5], hours_remaining=2, unit="C", sigma_fixed=1.5, obs_gap=0.7)
    assert max(tight.values()) > max(wide.values())
    assert min(wide) == 20 and min(tight) == 20               # never below the observed high


def test_pooled_uses_only_earlier_days():
    d0 = date(2026, 9, 1)
    mk = lambda d, v: ib.Case(d, None, None, 0, [], "", None, [v, v])
    p = ib.Pooled([mk(d0, 1.0), mk(d0 + timedelta(days=1), 3.0), mk(d0 + timedelta(days=1), 5.0)])
    assert p.before(d0) is None
    assert p.before(d0 + timedelta(days=1)) == [1.0, 1.0]
    assert p.before(d0 + timedelta(days=5)) == [3.0, 3.0] == p.all()
