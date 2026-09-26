"""
Jev (TypeSafe System One) — two semantic judgments per city, via the official SDK.

    pip install typesafe-sdk        # import typesafe_sdk
    env: TYPESAFE_API_KEY           # (JEV_API_KEY also accepted)

Jev is not a chat model: it takes a `state` (our evidence) and typed questions, and returns
calibrated probabilities. TypeSafe's docs are explicit that Jev "is not a calculator" and
cannot reliably compare numbers, so:
  * all arithmetic happens in code (weatherbot/probability.py);
  * the state is written as short semantic descriptions computed by code
    ("running much warmer than the models expected", "late afternoon; the usual peak has
    likely passed") plus the raw text Jev is good at reading (METAR, TAF, NWS discussion);
  * Jev answers two questions whose options are words, not numbers:
      vs_models   (Choice): much_cooler / cooler / as_forecast / warmer / much_warmer
      peak_passed (Noul):   has today's maximum most likely already occurred?
The code converts those probabilities into shifts of the temperature distribution.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy

from ..models import JevEvaluation, MetarObservation, Station
from ..settings import settings
from ..weather.forecast import StationForecast
from ..weather.taf import Taf

log = structlog.get_logger(__name__)

VS_MODELS = Choice(
    instructions=(
        "Weather forecast models have predicted today's maximum temperature at this airport "
        "weather station. Using the observations so far, the airport forecast (TAF) and any "
        "forecaster discussion, where will today's actual maximum temperature end up compared "
        "with what the models expect?"
    ),
    criteria={
        "much_cooler": {
            "what": "Clearly cooler than the models expect.",
            "signs": "Thick cloud, rain, fog or a cold front arriving before the usual afternoon "
                     "peak; a sea breeze or onshore wind the models underplay; station already "
                     "running much cooler than modelled.",
        },
        "cooler": {
            "what": "Somewhat cooler than the models expect.",
            "signs": "More cloud or showers than forecast, or the station running a little cool.",
        },
        "as_forecast": {
            "what": "About what the models expect.",
            "signs": "Observations tracking the forecast; nothing in the TAF or discussion "
                     "suggests the models are wrong.",
        },
        "warmer": {
            "what": "Somewhat warmer than the models expect.",
            "signs": "More sunshine than forecast, clouds clearing early, or the station running "
                     "a little warm.",
        },
        "much_warmer": {
            "what": "Clearly warmer than the models expect.",
            "signs": "Strong sunshine with dry air, downslope or offshore winds, a warm air mass "
                     "the models underdo; station already running much warmer than modelled.",
        },
    },
)

PEAK_PASSED = Noul(
    instructions="Has today's maximum temperature at this station most likely already been reached?",
    criteria={
        "true": "The day's peak is behind us: it is late in the day, temperatures are falling or "
                "cloud, rain or a cooler wind has arrived, and no further warming is expected.",
        "false": "More warming is still likely today: daytime heating continues or warmer air is "
                 "still arriving.",
    },
)
QUESTIONS = {"vs_models": VS_MODELS, "peak_passed": PEAK_PASSED}
JEV_SHIFT_LABELS = tuple(VS_MODELS.criteria)   # must match probability.JEV_SHIFT_STEPS


# ----------------------------------------------------------------- semantic state (code does numbers)
def _time_phrase(local_hour: int) -> str:
    if local_hour < 9:
        return "early morning; the main daytime heating is still ahead"
    if local_hour < 12:
        return "late morning; several hours of daytime heating remain"
    if local_hour < 15:
        return "early-to-mid afternoon; around the usual time of the daily peak"
    if local_hour < 18:
        return "late afternoon; the usual daily peak has probably passed"
    return "evening; little or no daytime heating remains"


def _bias_phrase(fc: StationForecast, obs_high: float, unit: str) -> str:
    step = 1.8 if unit == "F" else 1.0
    biases = [obs_high - m.elapsed_high for m in fc.models.values()
              if m.elapsed_high is not None and m.hours_elapsed >= 3]
    if not biases:
        return "too early in the day to compare observations with the models"
    b = statistics.median(biases) / step
    if b >= 1.5:
        return "running much warmer than the models expected so far today"
    if b >= 0.5:
        return "running somewhat warmer than the models expected so far today"
    if b <= -1.5:
        return "running much cooler than the models expected so far today"
    if b <= -0.5:
        return "running somewhat cooler than the models expected so far today"
    return "running about as the models expected so far today"


def _agreement_phrase(spread: float | None, unit: str) -> str:
    if spread is None:
        return "only one model available"
    s = spread / (1.8 if unit == "F" else 1.0)
    if s < 1.0:
        return "the forecast models agree closely"
    if s < 2.5:
        return "the forecast models disagree moderately"
    return "the forecast models disagree strongly"


def _outlook_phrase(fc: StationForecast, obs_high: float, unit: str) -> str:
    step = 1.8 if unit == "F" else 1.0
    rem = [m.remaining_high for m in fc.models.values() if m.remaining_high is not None]
    if not rem:
        return "the models show no hours of the day left"
    d = (statistics.median(rem) - obs_high) / step
    if d >= 1.5:
        return "the models expect it to get clearly warmer than the high recorded so far"
    if d >= 0.5:
        return "the models expect it to get a little warmer than the high recorded so far"
    return "the models do not expect it to get warmer than the high recorded so far"


_AFD_SECTION = re.compile(
    r"^\.(KEY MESSAGES|SYNOPSIS|NEAR TERM|SHORT TERM)[^\n]*\n(.*?)(?=^&&|^\.[A-Z])", re.M | re.S
)


def trim_afd(text: str, limit: int = 5000) -> str:
    """Keep only the sections about today (long unrelated state hurts Jev's accuracy)."""
    parts = [f"{m.group(1)}: {m.group(2).strip()}" for m in _AFD_SECTION.finditer(text)]
    out = "\n\n".join(parts) if parts else text
    return out[:limit]


def build_state(
    *, station: Station, metar: MetarObservation, forecast: StationForecast, taf: Taf | None,
    afd: tuple[datetime, str] | None, now: datetime,
) -> dict:
    local = now.astimezone(ZoneInfo(station.timezone))
    u = f"°{station.unit}"
    state = {
        "station": f"{station.city_label} — {station.airport_name} ({station.icao_code})",
        "local_time": f"{local.strftime('%A %H:%M')} — {_time_phrase(local.hour)}",
        "observations": (
            f"Highest temperature so far today: {metar.high_so_far:.0f}{u}. "
            f"Latest reading: {metar.temp:.0f}{u}. "
            f"The station is {_bias_phrase(forecast, metar.high_so_far, station.unit)}."
        ),
        "models": (f"{_agreement_phrase(forecast.spread(metar.high_so_far), station.unit).capitalize()}; "
                   f"{_outlook_phrase(forecast, metar.high_so_far, station.unit)}."),
        "latest_weather_report": metar.raw or "",
        "airport_forecast": taf.raw if taf else "not available",
    }
    if afd:
        state["forecaster_discussion"] = trim_afd(afd[1])
    return state


# ----------------------------------------------------------------- client
class JevClient:
    def __init__(self) -> None:
        self._client: AsyncTypeSafeClient | None = None

    def _get(self) -> AsyncTypeSafeClient:
        if self._client is None:
            settings.require("jev_api_key")
            self._client = AsyncTypeSafeClient(
                api_key=settings.jev_api_key,
                model=settings.jev_model,
                retry=RetryPolicy(max_retries=2, timeout=30.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def evaluate(self, state: dict) -> JevEvaluation:
        res = await self._get().system_one(state=state, questions=QUESTIONS)
        choice = res.choices["vs_models"]
        p_peak = float(res.nouls["peak_passed"].noul)
        probs = {k: float(v) for k, v in choice.probabilities.items()}
        warm = probs.get("warmer", 0) + probs.get("much_warmer", 0)
        cool = probs.get("cooler", 0) + probs.get("much_cooler", 0)
        direction = "neutral" if choice.choice == "as_forecast" else ("warmer" if warm >= cool else "cooler")
        return JevEvaluation(
            direction=direction,
            confidence=float(choice.confidence),
            rationale=f"vs_models={choice.choice} ({choice.confidence:.2f}); peak_passed p={p_peak:.2f}",
            shift_probs=probs,
            p_peak_passed=p_peak,
            afd_excerpt=(state.get("forecaster_discussion") or "")[:2000] or None,
            raw={"model": res.model, "request_id": res.request_id,
                 "vs_models": {"choice": choice.choice, "confidence": choice.confidence, "probabilities": probs},
                 "peak_passed": p_peak,
                 "usage": res.usage.model_dump() if res.usage else None},
        )
