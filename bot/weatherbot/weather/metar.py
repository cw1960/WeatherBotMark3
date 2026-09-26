"""
Ground truth: METAR observations for exact ICAO stations, worldwide.

Source: aviationweather.gov Data API (NOAA), one request for every station:
    GET https://aviationweather.gov/api/data/metar?ids=KLGA,EGLC,...&format=json&hours=36

Rules:
  * We only ever query by the station's icao_code. No city-level lookups.
  * US METARs carry a remarks T-group (e.g. T01500094 = 15.0°C) with tenths of a degree;
    we prefer it over the rounded body temperature so °F conversion is exact.
  * The running daily high is computed over observations since *local* midnight at the
    station, because that is the day the market resolves on.
"""
from __future__ import annotations

import re
from math import ceil
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..models import MetarObservation, Station
from ..settings import settings

log = structlog.get_logger(__name__)

AWC_METAR = "https://aviationweather.gov/api/data/metar"
AWC_MAX_RESULTS = 400   # documented cap: "Most endpoints return a maximum of 400 entries"
BATCH = 4               # 4 stations × ≤26 h × ≤3 reports/h stays under the cap
_TGROUP = re.compile(r"\bT([01])(\d{3})[01]\d{3}\b")
# US synoptic remark: 6-hour maximum temperature, e.g. "10267" = +26.7°C (sign digit 0/1).
# Can exceed the hourly readings; recorded as context only (see summarise).
_SIXHR_MAX = re.compile(r"\b1([01])(\d{3})\b")


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def precise_temp_c(item: dict) -> float | None:
    """Tenths-precision temp from the T-group if present, else the body temperature."""
    raw = item.get("rawOb") or ""
    if m := _TGROUP.search(raw):
        sign = -1 if m.group(1) == "1" else 1
        return sign * int(m.group(2)) / 10
    t = item.get("temp")
    return float(t) if t is not None else None


def six_hour_max_c(item: dict) -> float | None:
    raw = item.get("rawOb") or ""
    if "RMK" not in raw:
        return None
    if m := _SIXHR_MAX.search(raw.split("RMK", 1)[1]):
        sign = -1 if m.group(1) == "1" else 1
        return sign * int(m.group(2)) / 10
    return None


def to_unit(temp_c: float, unit: str) -> float:
    return round(c_to_f(temp_c), 1) if unit == "F" else round(temp_c, 1)


class MetarClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _fetch(self, icaos: list[str], hours: int) -> list[dict]:
        r = await self._http.get(
            AWC_METAR,
            params={"ids": ",".join(icaos), "format": "json", "hours": hours},
            headers={"User-Agent": settings.user_agent},
            timeout=30,
        )
        if r.status_code == 204:  # no data
            return []
        r.raise_for_status()
        return r.json()

    async def _fetch_complete(self, icaos: list[str], hours: int) -> list[dict]:
        """Fetch, and if the reply hits the API's 400-entry cap (it silently drops the OLDEST
        reports — often the ones holding the day's high), split the batch and try again."""
        items = await self._fetch(icaos, hours)
        if len(items) < AWC_MAX_RESULTS:
            return items
        if len(icaos) == 1:
            log.error("metar_truncated_single_station", icao=icaos[0], hours=hours)
            return items
        mid = len(icaos) // 2
        return (await self._fetch_complete(icaos[:mid], hours)
                + await self._fetch_complete(icaos[mid:], hours))

    async def fetch_all(self, stations: list[Station], now: datetime | None = None) -> dict[str, MetarObservation]:
        """Latest reading + running local-day high for every station, in each station's unit.
        Stations with no report since their local midnight are omitted.

        aviationweather.gov returns at most 400 entries per request, newest first. Asking for
        40 stations × 36 h silently lost the midday reports (first live run: Karachi 33°C
        missed, reported 32°C). So: small batches, a window just covering each station's
        local day, and automatic splitting if a reply still hits the cap."""
        now = now or datetime.now(timezone.utc)
        by_icao: dict[str, list[dict]] = defaultdict(list)
        # Order by hours needed so each batch's window is tight.
        need = sorted(
            {s.icao_code: max(1, ceil((now - self.local_day_start_utc(s, now.astimezone(ZoneInfo(s.timezone)).date())).total_seconds() / 3600) + 1)
             for s in stations}.items(),
            key=lambda kv: kv[1],
        )
        for i in range(0, len(need), BATCH):
            chunk = need[i:i + BATCH]
            hours = max(h for _, h in chunk)
            for item in await self._fetch_complete([icao for icao, _ in chunk], hours):
                by_icao[item.get("icaoId", "")].append(item)

        out: dict[str, MetarObservation] = {}
        for st in stations:
            obs = self.summarise(st, by_icao.get(st.icao_code, []))
            if obs is not None:
                out[st.icao_code] = obs
            else:
                log.warning("metar_missing_today", icao=st.icao_code, city=st.city_slug)
        return out

    @staticmethod
    def local_day_start_utc(st: Station, day: date | None = None) -> datetime:
        tz = ZoneInfo(st.timezone)
        day = day or datetime.now(tz).date()
        return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)

    @classmethod
    def summarise(cls, st: Station, items: list[dict], day: date | None = None) -> MetarObservation | None:
        start = cls.local_day_start_utc(st, day)
        today: list[tuple[datetime, float, str]] = []
        six_hr_maxes: list[float] = []
        for it in items:
            ts = it.get("obsTime")
            t_c = precise_temp_c(it)
            if ts is None or t_c is None:
                continue
            obs_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if obs_at >= start:
                today.append((obs_at, t_c, it.get("rawOb", "")))
                # Only trust a 6-hour max whose whole window lies inside today (local).
                if (mx := six_hour_max_c(it)) is not None and obs_at - timedelta(hours=6) >= start:
                    six_hr_maxes.append(mx)
        if not today:
            return None
        today.sort(key=lambda x: x[0])
        latest_at, latest_c, latest_raw = today[-1]
        # Settlement uses the reported readings ("Temp" column), NOT the 6-hour max group, so the
        # group is kept only as context for Jev / the audit log.
        high_c = max(t for _, t, _ in today)
        return MetarObservation(
            icao_code=st.icao_code,
            observed_at=latest_at,
            temp=to_unit(latest_c, st.unit),
            high_so_far=to_unit(high_c, st.unit),
            unit=st.unit,
            n_obs_today=len(today),
            raw=latest_raw,
            six_hour_max=to_unit(max(six_hr_maxes), st.unit) if six_hr_maxes else None,
        )
