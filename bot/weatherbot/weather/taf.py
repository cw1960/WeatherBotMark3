"""
Terminal Aerodrome Forecasts (TAFs) — the airport's own official short-range forecast.

Source: aviationweather.gov Data API, one request for every station:
    GET https://aviationweather.gov/api/data/taf?ids=KLGA,EGLC,...&format=json

Why it's useful even without a temperature: a TAF spells out cloud cover, rain, fog and
wind timing hour by hour (e.g. "FM261900 ... RA OVC015" = rain and low overcast from 19Z),
which is exactly what caps or boosts a daily high. Jev reads the raw text.

Many non-US TAFs also carry explicit temperature groups, e.g.
    TX31/2614Z TN22/2705Z   → max 31°C at 14Z on the 26th, min 22°C at 05Z on the 27th
(US TAFs essentially never include them.) We parse TX when present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..settings import settings

log = structlog.get_logger(__name__)

AWC_TAF = "https://aviationweather.gov/api/data/taf"
_TX = re.compile(r"\bTX(M?)(\d{2})/(\d{2})(\d{2})Z\b")


@dataclass(frozen=True)
class Taf:
    icao_code: str
    issued: str
    raw: str
    max_temp_c: float | None       # from TX group, if present
    max_temp_day: int | None       # day-of-month of the TX group
    max_temp_hour_utc: int | None


def parse_tx(raw: str) -> tuple[float | None, int | None, int | None]:
    if m := _TX.search(raw):
        sign = -1 if m.group(1) == "M" else 1
        return sign * float(m.group(2)), int(m.group(3)), int(m.group(4))
    return None, None, None


class TafClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _fetch(self, icaos: list[str]) -> list[dict]:
        r = await self._http.get(
            AWC_TAF, params={"ids": ",".join(icaos), "format": "json"},
            headers={"User-Agent": settings.user_agent}, timeout=30,
        )
        if r.status_code == 204:
            return []
        r.raise_for_status()
        return r.json()

    async def fetch_all(self, icaos: list[str]) -> dict[str, Taf]:
        out: dict[str, Taf] = {}
        uniq = sorted(set(icaos))
        for i in range(0, len(uniq), 40):
            for item in await self._fetch(uniq[i:i + 40]):
                icao = item.get("icaoId", "")
                raw = (item.get("rawTAF") or "").strip()
                if not icao or not raw:
                    continue
                # Keep the most recent TAF per station.
                prev = out.get(icao)
                issued = item.get("issueTime", "")
                if prev and prev.issued >= issued:
                    continue
                tx, day, hour = parse_tx(raw)
                out[icao] = Taf(icao, issued, raw, tx, day, hour)
        return out
