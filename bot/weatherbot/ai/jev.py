"""
Jev System One — reads the full evidence bundle for ONE city and returns a verdict.

Runs for every city. The bundle (see ai/evidence.py) contains observations, every model's
projected high, the ensemble spread, the airport TAF, and — US only — the NWS forecaster
discussion. Jev's job is judgment the numbers can't do alone: which model to trust today,
whether the TAF/AFD describe cloud, rain, fronts or sea breezes the models are mishandling.

API CONTRACT — NOT YET CONFIRMED. This assumes an OpenAI-compatible chat endpoint
(POST {JEV_API_URL}/chat/completions, Bearer key, JSON response_format). Once Jev's real
API docs are available, only `_call()` should need to change.
"""
from __future__ import annotations

import json

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..models import JevEvaluation
from ..settings import settings

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are an expert operational meteorologist forecasting ONE number: the
maximum temperature that will be recorded today at a specific airport weather station, in
the station's own unit, rounded to a whole degree. Polymarket settles on that number.

You receive a JSON evidence bundle:
- observations: the high recorded so far today (it can only go up) and the latest METAR
- models: every numerical model's projected final high, their consensus (median) and spread
- taf: the airport's official forecast text — use its cloud, precipitation, wind and timing
- afd: the local forecasters' written discussion (US stations only; may be null)
- market: the settlement buckets and which bucket each model points to

Reason about: how much daytime heating remains at the local time given; whether cloud, rain,
fog, fronts, sea breezes or downslope winds in the TAF/AFD argue for a model being too warm
or too cool; which models have been handling this setup best; whether the high may already
be in. Do NOT restate the evidence.

Respond with strict JSON only:
{"expected_high": <number>,
 "direction": "warmer" | "cooler" | "neutral" | "uncertain",   // vs models.consensus_projected_high
 "confidence": <0.0-1.0>,
 "rationale": "<2-3 sentences>"}"""


class JevClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def _call(self, evidence: dict) -> tuple[dict, dict]:
        settings.require("jev_api_key")
        payload = {
            "model": settings.jev_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(evidence, default=str)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        r = await self._http.post(
            f"{settings.jev_api_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.jev_api_key}"},
            timeout=90,
        )
        r.raise_for_status()
        body = r.json()
        return json.loads(body["choices"][0]["message"]["content"]), body

    async def evaluate(self, evidence: dict) -> JevEvaluation:
        parsed, body = await self._call(evidence)
        afd = evidence.get("afd") or {}
        return JevEvaluation(
            direction=parsed["direction"],
            confidence=float(parsed["confidence"]),
            rationale=parsed.get("rationale", ""),
            expected_high=parsed.get("expected_high"),
            afd_excerpt=(afd.get("text") or "")[:2000] or None,
            raw={"verdict": parsed, "response_id": body.get("id")},
        )
