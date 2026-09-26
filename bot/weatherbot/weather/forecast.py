"""
Numerical model forecast of the daily high at the station point.

NEXT TASK — not implemented yet. Design notes:
  * HRRR (3 km, hourly) covers only the continental US → use it for the 11 US cities.
  * The other 40 cities need a global model (e.g. ECMWF IFS 0.25°, DWD ICON, GFS).
  * Open-Meteo serves all of these by lat/lon in one JSON API, which is the planned source.
  * Always query by the station's catalogued lat/lon — never by city name.
Until implemented, forecast_high() returns None and the bot logs a 'skip' row.
"""
from __future__ import annotations

from datetime import date

import httpx
import structlog

from ..models import ModelForecast, Station

log = structlog.get_logger(__name__)


def model_for(station: Station) -> str:
    return "hrrr" if station.country == "US" else "ecmwf_ifs025"


class ForecastClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def forecast_high(self, station: Station, target_date: date) -> ModelForecast | None:
        log.debug("forecast_not_implemented", icao=station.icao_code, model=model_for(station))
        return None
