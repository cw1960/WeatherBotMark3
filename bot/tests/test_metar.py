"""METAR parsing: T-group precision, unit conversion, local-midnight daily high."""
from datetime import date, datetime, timezone

from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.weather.metar import MetarClient, precise_temp_c

NYC = to_station(BY_SLUG["nyc"])        # America/New_York, °F
LON = to_station(BY_SLUG["london"])     # Europe/London, °C


def _item(ts: datetime, temp, raw=""):
    return {"icaoId": "X", "obsTime": int(ts.timestamp()), "temp": temp, "rawOb": raw}


def test_tgroup_beats_rounded_body_temp():
    raw = "METAR KLGA 260251Z 02025G40KT 7SM RA OVC110 15/09 A2996 RMK AO2 T01560094"
    assert precise_temp_c({"temp": 16, "rawOb": raw}) == 15.6
    assert precise_temp_c({"temp": -2, "rawOb": "... RMK T10220094"}) == -2.2
    assert precise_temp_c({"temp": 16, "rawOb": "METAR EGLC 16/10 Q1021"}) == 16.0


def test_daily_high_uses_local_midnight_and_station_unit():
    # NYC local 2026-09-26 00:00 EDT = 04:00 UTC. The 03:00 UTC reading belongs to the 25th.
    items = [
        _item(datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc), 30, "RMK T03000100"),   # yesterday, hot
        _item(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc), 20, "RMK T02000100"),
        _item(datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc), 22, "RMK T02220100"),  # 22.2°C = 72.0°F
        _item(datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc), 21, "RMK T02110100"),
    ]
    o = MetarClient.summarise(NYC, items, day=date(2026, 9, 26))
    assert o.unit == "F" and o.n_obs_today == 3
    assert o.high_so_far == 72.0          # not 86°F from the previous evening
    assert o.temp == 70.0                 # latest: 21.1°C


def test_celsius_station_stays_celsius():
    items = [_item(datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc), 19)]
    o = MetarClient.summarise(LON, items, day=date(2026, 9, 26))
    assert o.unit == "C" and o.high_so_far == 19.0


def test_no_obs_today_returns_none():
    assert MetarClient.summarise(NYC, [], day=date(2026, 9, 26)) is None
