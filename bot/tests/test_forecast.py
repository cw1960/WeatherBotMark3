"""Multi-model forecast: day windows, remaining-hours max, consensus/spread, API robustness."""
from datetime import date, datetime, timedelta, timezone

import httpx

from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.weather import forecast as fcmod
from weatherbot.weather.forecast import ForecastClient, parse_location, summarise_hourly

NYC = to_station(BY_SLUG["nyc"])       # UTC-4 in September, °F
TOKYO = to_station(BY_SLUG["tokyo"])   # UTC+9, °C
DAY = date(2026, 9, 26)
NYC_MIDNIGHT_UTC = datetime(2026, 9, 26, 4, tzinfo=timezone.utc)


def _hours(start: datetime, n: int) -> list[int]:
    return [int((start + timedelta(hours=i)).timestamp()) for i in range(n)]


def test_remaining_high_only_counts_future_hours_of_local_day():
    times = _hours(NYC_MIDNIGHT_UTC - timedelta(hours=6), 36)   # covers the whole local day
    # warm peak at local 14:00 (18Z), cooler later
    temps = []
    for t in times:
        h = datetime.fromtimestamp(t, tz=timezone.utc).hour
        temps.append(25.0 if h == 18 else 15.0)
    now = datetime(2026, 9, 26, 20, tzinfo=timezone.utc)  # local 16:00, peak already passed
    fc = summarise_hourly(NYC, times, {"gfs_hrrr": temps}, DAY, now)
    m = fc.models["gfs_hrrr"]
    assert m.full_day_high == 77.0             # 25°C
    assert m.remaining_high == 59.0            # only 15°C left today
    assert m.hours_remaining == 8              # 20Z .. 03Z (local 16:00-23:00)
    # observed high so far dominates what's left
    assert fc.projected_highs(observed_high=74.0)["gfs_hrrr"] == 74.0


def test_consensus_and_spread():
    times = _hours(NYC_MIDNIGHT_UTC, 24)
    series = {"a": [20.0] * 24, "b": [22.0] * 24, "c": [21.0] * 24}
    now = NYC_MIDNIGHT_UTC
    fc = summarise_hourly(NYC, times, series, DAY, now)
    # 20/21/22°C → 68.0/69.8/71.6°F
    assert fc.consensus(None) == 69.8
    assert fc.spread(None) == 3.6


def test_model_without_enough_hours_is_dropped():
    times = _hours(NYC_MIDNIGHT_UTC, 24)
    series = {"ok": [20.0] * 24, "abroad": [None] * 24}   # e.g. HRRR outside the US
    fc = summarise_hourly(NYC, times, series, DAY, NYC_MIDNIGHT_UTC)
    assert set(fc.models) == {"ok"}


def test_parse_location_both_key_styles():
    multi = {"hourly": {"time": [1, 2], "temperature_2m_ecmwf_ifs025": [1.0, 2.0], "temperature_2m_icon_seamless": [3.0, 4.0]}}
    t, s = parse_location(multi, ("ecmwf_ifs025", "icon_seamless", "gfs_seamless"))
    assert t == [1, 2] and set(s) == {"ecmwf_ifs025", "icon_seamless"}
    single = {"hourly": {"time": [1], "temperature_2m": [5.0]}}
    assert parse_location(single, ("gem_seamless",))[1] == {"gem_seamless": [5.0]}


async def test_rejected_model_is_dropped_and_rest_still_works(monkeypatch):
    monkeypatch.setattr(fcmod, "GLOBAL_MODELS", ("ecmwf_ifs025", "bogus_model"))
    times = _hours(datetime(2026, 9, 25, 15, tzinfo=timezone.utc), 72)  # Tokyo local days

    def handler(request: httpx.Request) -> httpx.Response:
        models = request.url.params["models"].split(",")
        if "bogus_model" in models:
            return httpx.Response(400, json={"error": True, "reason": "Invalid model bogus_model"})
        hourly = {"time": times}
        for m in models:
            hourly[f"temperature_2m_{m}"] = [24.0] * len(times)
        return httpx.Response(200, json={"hourly": hourly})

    client = ForecastClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    now = datetime(2026, 9, 26, 0, tzinfo=timezone.utc)   # 09:00 in Tokyo
    out = await client.fetch_all([TOKYO], now=now)
    fc = out[("RJTT", date(2026, 9, 26))]
    assert set(fc.models) == {"ecmwf_ifs025"}
    assert fc.models["ecmwf_ifs025"].full_day_high == 24.0
    assert "bogus_model" in client._rejected


def test_us_gets_hrrr_elsewhere_does_not():
    assert "gfs_hrrr" in fcmod.models_for(NYC)
    assert "gfs_hrrr" not in fcmod.models_for(TOKYO)


def test_bias_so_far_needs_elapsed_hours():
    from weatherbot.weather.forecast import ModelDay, StationForecast
    now = NYC_MIDNIGHT_UTC
    fc = StationForecast("KLGA", "F", DAY, now, {
        "a": ModelDay("a", 75, 75, 8, 70.0, 16), "b": ModelDay("b", 76, 76, 8, 71.0, 16)})
    assert fc.bias_so_far(73.0) == 2.5          # running warmer than both models
    early = StationForecast("KLGA", "F", DAY, now, {"a": ModelDay("a", 75, 75, 22, 60.0, 2)})
    assert early.bias_so_far(62.0) is None       # too early to judge
