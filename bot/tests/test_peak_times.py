"""Peak-time study: parsing, first-peak detection, window stats, settlement check, full run."""
import json
import sys
from datetime import date, datetime, timedelta

import httpx

import peak_times as pt
from weatherbot.catalog import BY_SLUG, to_station


def test_iem_ids():
    assert pt.iem_id(to_station(BY_SLUG["nyc"])) == "LGA"
    assert pt.iem_id(to_station(BY_SLUG["london"])) == "EGLC"


def test_parse_and_first_peak():
    csv = "station,valid,tmpc\n" + "\n".join(
        f"EGLC,2026-09-01 {h:02d}:50,{t}" for h, t in
        [(0, 15), (1, 15), (2, 14), (3, 14), (4, 14), (5, 14), (6, 15), (7, 16), (8, 17), (9, 18),
         (10, 19), (11, 20), (12, 21), (13, 22), (14, 22), (15, 22), (16, 21), (17, 20), (18, 19),
         (19, 18), (20, 17), (21, 17), (22, "M"), (23, 16)])
    rows = pt.parse_iem_csv(csv)
    assert len(rows) == 23                               # 'M' skipped
    peaks = pt.daily_peaks(rows, "C")
    assert peaks == {date(2026, 9, 1): (13 * 60 + 50, 22.0)}   # FIRST time 22 was reached
    assert pt.daily_peaks(rows[:10], "C") == {}                # incomplete day ignored


def test_summary_statistics():
    base = date(2026, 8, 1)
    peaks = {base + timedelta(days=i): (14 * 60 + (30 if i % 2 else -30), 20.0) for i in range(18)}
    peaks[base + timedelta(days=18)] = (8 * 60, 20.0)      # odd: morning peak (front)
    peaks[base + timedelta(days=19)] = (21 * 60, 20.0)     # odd: evening peak
    s = pt.summarise(peaks)
    assert s["n_days"] == 20 and s["median_peak"] in ("14:00", "14:30", "13:30")
    assert s["within_1h_of_median"] == 0.9 and s["odd_days"] == 0.1
    assert s["locked_by_13"] == 0.05 and s["locked_by_15"] == 0.95


async def test_full_run_with_fake_archive(tmp_path, monkeypatch, capsys):
    st = to_station(BY_SLUG["london"])
    end = datetime.now().date() - timedelta(days=2)
    days = [end - timedelta(days=i) for i in range(20)]

    def handler(req):
        lines = ["station,valid,tmpc"]
        for d in sorted(days):
            for h in range(24):
                t = 20 if h == 15 else 15
                lines.append(f"EGLC,{d} {h:02d}:50,{t}")
        return httpx.Response(200, text="\n".join(lines))

    # a settled market for one day, in backtest's cache format: winner 20°C
    from weatherbot.exchange.markets import event_slug
    gdir = tmp_path / "cache" / "gamma"
    gdir.mkdir(parents=True)
    labels = [("19°C or below", None, 19), ("20°C", 20, 20), ("21°C or higher", 21, None)]
    (gdir / f"{event_slug('london', days[0])}.json").write_text(json.dumps([{
        "id": 1, "slug": "s", "closed": True, "markets": [
            {"id": i, "conditionId": f"c{i}", "groupItemTitle": lab, "clobTokenIds": '["a","b"]',
             "outcomePrices": '["1","0"]' if lab == "20°C" else '["0","1"]'} for i, (lab, _, _) in enumerate(labels)]}]))

    real = httpx.AsyncClient
    monkeypatch.setattr(pt.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(pt, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(pt, "OUT", tmp_path / "peak_times.json")

    async def no_sleep(_):
        return None
    monkeypatch.setattr(pt.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(sys, "argv", ["peak_times.py", "london", "--days", "20"])
    assert await pt.main() == 0
    saved = json.loads((tmp_path / "peak_times.json").read_text())["stations"]["EGLC"]
    assert saved["median_peak"] == "15:50" and saved["within_1h_of_median"] == 1.0
    assert saved["archive_matches_settlement"] == 1.0 and saved["checked_days"] == 1
    assert "15:50" in capsys.readouterr().out
