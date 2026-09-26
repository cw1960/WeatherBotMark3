"""
Area Forecast Discussion (AFD) fetcher — the meteorologist prose Jev evaluates.

GET https://api.weather.gov/products/types/AFD/locations/{WFO}  → list of product ids
GET https://api.weather.gov/products/{id}                        → productText
"""
from __future__ import annotations

from datetime import datetime

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..settings import settings

log = structlog.get_logger(__name__)

NWS_BASE = "https://api.weather.gov"


class AfdClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def latest(self, wfo: str) -> tuple[datetime, str] | None:
        headers = {"User-Agent": settings.user_agent}
        r = await self._http.get(f"{NWS_BASE}/products/types/AFD/locations/{wfo}", headers=headers)
        r.raise_for_status()
        products = r.json().get("@graph", [])
        if not products:
            return None
        newest = products[0]
        r2 = await self._http.get(f"{NWS_BASE}/products/{newest['id']}", headers=headers)
        r2.raise_for_status()
        body = r2.json()
        issued = datetime.fromisoformat(body["issuanceTime"].replace("Z", "+00:00"))
        return issued, body["productText"]
