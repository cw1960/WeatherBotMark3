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
    ours = float(sixty.split("Bot (readings + models)")[1].split("log loss=")[1].split()[0])
    assert ours < 1.0                                          # readings make the bot sharp


async def test_no_edge_when_market_already_knows(tmp_path, monkeypatch):
    report = await _world(tmp_path, monkeypatch, lambda lab, win: 0.97 if lab == win else 0.002)
    assert "Edge test: NO" in report.split("=== 60 minutes")[1]
