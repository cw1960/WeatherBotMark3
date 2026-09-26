"""
Jev System One — sentiment evaluation of an Area Forecast Discussion.

Plain httpx; no SDK. The endpoint path/payload below is a reasonable default —
adjust to Jev's actual API contract once confirmed.
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..models import JevEvaluation
from ..settings import settings

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a meteorology analyst. You will be given a National Weather Service
Area Forecast Discussion (AFD), the current observed running daily high at a specific METAR
station, and the HRRR model's forecast high for that station.

Decide whether the forecasters' language suggests the actual daily high will end up
WARMER, COOLER, or NEUTRAL relative to the HRRR forecast high, or UNCERTAIN if the AFD
does not address it. Weigh explicit statements about model biases, cloud cover timing,
frontal passage, mixing, and forecaster confidence.

Respond with strict JSON only:
{"direction": "warmer|cooler|neutral|uncertain", "confidence": 0.0-1.0, "rationale": "<2-3 sentences>"}"""


class JevClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    async def evaluate_afd(
        self,
        *,
        icao_code: str,
        afd_text: str,
        afd_issued_at: datetime,
        observed_high_f: float,
        hrrr_high_f: float,
    ) -> JevEvaluation:
        user_msg = (
            f"Station: {icao_code}\n"
            f"Observed running high so far: {observed_high_f:.1f}°F\n"
            f"HRRR forecast high: {hrrr_high_f:.1f}°F\n\n"
            f"--- AFD (issued {afd_issued_at.isoformat()}) ---\n{afd_text[:12000]}"
        )
        payload = {
            "model": settings.jev_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        r = await self._http.post(
            f"{settings.jev_api_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.jev_api_key}"},
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return JevEvaluation(
            direction=parsed["direction"],
            confidence=float(parsed["confidence"]),
            rationale=parsed.get("rationale", ""),
            afd_issued_at=afd_issued_at,
            afd_excerpt=afd_text[:2000],
            raw=body,
        )
