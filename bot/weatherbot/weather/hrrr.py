"""
Numerical model: HRRR forecast daily high for the station point.

Options (pick one when implementing):
  * NOMADS GRIB2 subset (grib_filter) → decode with cfgrib/xarray at the station lat/lon
  * Open-Meteo HRRR endpoint (JSON, easiest to start):
        https://api.open-meteo.com/v1/gfs?models=hrrr... (check current param names)
  * NWS gridpoint hourly forecast as a proxy when HRRR is unavailable

Whatever the source, the query must be by station coordinates, not by city name.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import structlog

from ..models import HrrrForecast, Station

log = structlog.get_logger(__name__)


class HrrrClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def forecast_high(self, station: Station, target_date: date) -> HrrrForecast | None:
        # TODO: implement real HRRR fetch. Placeholder returns None so the loop skips gracefully.
        log.debug("hrrr_not_implemented", icao=station.icao_code, target_date=str(target_date))
        return None
