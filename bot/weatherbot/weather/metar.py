"""
Ground truth: latest METAR observation for an exact ICAO station.

Primary source: NWS API  GET https://api.weather.gov/stations/{ICAO}/observations/latest
Fallback:       MADIS / Synoptic (token required) — TODO.

Rule: we only ever query by the station's icao_code. No city-level lookups.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..models import MetarObservation
from ..settings import settings

log = structlog.get_logger(__name__)

NWS_BASE = "https://api.weather.gov"


def _c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


class MetarClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def latest(self, icao_code: str) -> MetarObservation | None:
        url = f"{NWS_BASE}/stations/{icao_code}/observations/latest"
        r = await self._http.get(url, headers={"User-Agent": settings.nws_user_agent})
        if r.status_code == 404:
            log.warning("metar_station_not_found", icao=icao_code)
            return None
        r.raise_for_status()
        props = r.json()["properties"]
        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None:
            return None
        observed_at = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
        temp_f = _c_to_f(temp_c)
        return MetarObservation(
            icao_code=icao_code,
            observed_at=observed_at,
            temp_f=temp_f,
            high_so_far_f=temp_f,  # refined by daily_high()
            raw=props.get("rawMessage"),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def daily_high(self, icao_code: str, local_day_start_utc: datetime) -> float | None:
        """Max temperature observed since local midnight (the value Polymarket resolves on)."""
        url = f"{NWS_BASE}/stations/{icao_code}/observations"
        params = {"start": local_day_start_utc.astimezone(timezone.utc).isoformat(), "limit": 500}
        r = await self._http.get(url, params=params, headers={"User-Agent": settings.nws_user_agent})
        r.raise_for_status()
        temps = [
            f["properties"]["temperature"]["value"]
            for f in r.json().get("features", [])
            if f["properties"].get("temperature", {}).get("value") is not None
        ]
        return _c_to_f(max(temps)) if temps else None
