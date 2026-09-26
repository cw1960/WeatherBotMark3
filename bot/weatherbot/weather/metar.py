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
from collections import defaultdict
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..models import MetarObservation, Station
from ..settings import settings

log = structlog.get_logger(__name__)

AWC_METAR = "https://aviationweather.gov/api/data/metar"
_TGROUP = re.compile(r"\bT([01])(\d{3})[01]\d{3}\b")


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

    async def fetch_all(self, stations: list[Station], hours: int = 36) -> dict[str, MetarObservation]:
        """Latest reading + running local-day high for every station, in each station's unit.
        Stations with no report since their local midnight are omitted."""
        by_icao: dict[str, list[dict]] = defaultdict(list)
        # The API accepts long id lists; chunk defensively.
        icaos = sorted({s.icao_code for s in stations})
        for i in range(0, len(icaos), 40):
            for item in await self._fetch(icaos[i:i + 40], hours):
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
        for it in items:
            ts = it.get("obsTime")
            t_c = precise_temp_c(it)
            if ts is None or t_c is None:
                continue
            obs_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if obs_at >= start:
                today.append((obs_at, t_c, it.get("rawOb", "")))
        if not today:
            return None
        today.sort(key=lambda x: x[0])
        latest_at, latest_c, latest_raw = today[-1]
        high_c = max(t for _, t, _ in today)
        return MetarObservation(
            icao_code=st.icao_code,
            observed_at=latest_at,
            temp=to_unit(latest_c, st.unit),
            high_so_far=to_unit(high_c, st.unit),
            unit=st.unit,
            n_obs_today=len(today),
            raw=latest_raw,
        )
