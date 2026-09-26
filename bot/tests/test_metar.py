"""METAR parsing: T-group precision, unit conversion, local-midnight daily high."""
from datetime import date, datetime, timedelta, timezone

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


def test_six_hour_max_is_context_not_settlement():
    # Hourly reports peaked at 22.2°C; the 18Z synoptic report says the 6-hour max was 23.3°C.
    # Markets settle on the reported readings, so the high stays 72.0°F; 23.3°C is kept as context.
    items = [
        _item(datetime(2026, 9, 26, 17, 0, tzinfo=timezone.utc), 22, "METAR KLGA 261651Z RMK AO2 T02220100"),
        _item(datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc), 22, "METAR KLGA 261751Z RMK AO2 SLP144 T02170100 10233 20150 58003"),
    ]
    o = MetarClient.summarise(NYC, items, day=date(2026, 9, 26))
    assert o.high_so_far == 72.0 and o.six_hour_max == 73.9


def test_six_hour_max_spanning_midnight_is_ignored():
    # 06Z report's 6-h window starts 00Z = 20:00 the previous local evening → not today's.
    items = [_item(datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc), 18, "METAR KLGA RMK AO2 T01800100 10290")]
    o = MetarClient.summarise(NYC, items, day=date(2026, 9, 26))
    assert o.high_so_far == 64.4 and o.six_hour_max is None   # window starts before local midnight


# ---------------------------------------------------------------- the 400-entry cap (live bug)
import httpx
from zoneinfo import ZoneInfo
from weatherbot.catalog import CATALOG
from weatherbot.weather import metar as metar_mod


def _capped_api(peaks: dict[str, float], now: datetime, calls: list):
    """Fake aviationweather.gov: half-hourly reports for 48 h; peak of the local day at 13:00;
    returns only the NEWEST 400 entries across all requested ids, like the real API."""
    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        hours = int(request.url.params["hours"])
        calls.append((len(ids), hours))
        items = []
        for icao in ids:
            st = next(to_station(e) for e in CATALOG if e.icao == icao)
            tz = ZoneInfo(st.timezone)
            t = now
            while t > now - timedelta(hours=hours):
                local = t.astimezone(tz)
                temp = peaks[icao] if (local.hour == 13 and local.minute < 30 and local.date() == now.astimezone(tz).date()) else peaks[icao] - 4
                items.append({"icaoId": icao, "obsTime": int(t.timestamp()), "temp": temp, "rawOb": f"METAR {icao}"})
                t -= timedelta(minutes=30)
        items.sort(key=lambda it: -it["obsTime"])
        return httpx.Response(200, json=items[:metar_mod.AWC_MAX_RESULTS])
    return handler


async def test_every_station_keeps_its_midday_peak_despite_the_cap():
    from datetime import timedelta  # noqa: F811
    stations = [to_station(e) for e in CATALOG if e.active]
    now = datetime(2026, 9, 26, 16, 38, tzinfo=timezone.utc)   # the moment of the live report
    peaks = {s.icao_code: 30.0 for s in stations}
    calls: list = []
    client = MetarClient(httpx.AsyncClient(transport=httpx.MockTransport(_capped_api(peaks, now, calls))))
    obs = await client.fetch_all(stations, now=now)
    karachi = obs["OPKC"]                                        # 21:38 local, peak was at 13:00
    assert karachi.high_so_far == 30.0
    missed = [s.city_slug for s in stations
              if s.icao_code in obs
              and datetime.combine(now.astimezone(ZoneInfo(s.timezone)).date(), datetime.min.time().replace(hour=13),
                                   tzinfo=ZoneInfo(s.timezone)) <= now
              and obs[s.icao_code].high_so_far != (86.0 if s.unit == "F" else 30.0)]
    assert missed == [], f"peaks lost to truncation: {missed}"
    assert all(n <= metar_mod.BATCH for n, _ in calls)


async def test_oversized_reply_is_split():
    calls: list = []
    now = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
    peaks = {"OPKC": 33.0, "VILK": 26.0}
    client = MetarClient(httpx.AsyncClient(transport=httpx.MockTransport(_capped_api(peaks, now, calls))))
    # ask for a huge window directly: 2 stations × 300 h × 2/h ≫ 400 → must split to singles
    items = await client._fetch_complete(["OPKC", "VILK"], 300)
    assert {n for n, _ in calls} == {2, 1}
    assert {it["icaoId"] for it in items} == {"OPKC", "VILK"}
