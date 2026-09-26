"""Jev integration: semantic state (no raw number comparisons asked of Jev) and response parsing."""
import json
from datetime import date, datetime, timezone

import httpx2

from weatherbot.ai import jev as jevmod
from weatherbot.ai.jev import JevClient, build_state, trim_afd
from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.models import MetarObservation
from weatherbot.weather.forecast import ModelDay, StationForecast

NYC = to_station(BY_SLUG["nyc"])


def _inputs():
    now = datetime(2026, 9, 26, 20, 30, tzinfo=timezone.utc)          # 16:30 in New York
    metar = MetarObservation(icao_code="KLGA", observed_at=now, temp=74, high_so_far=76.0, unit="F",
                             raw="METAR KLGA 262051Z 20008KT 10SM FEW250 24/12 A3001 RMK AO2 T02440122")
    fc = StationForecast("KLGA", "F", date(2026, 9, 26), now, {
        "gfs_hrrr": ModelDay("gfs_hrrr", 73.0, 72.0, 7, 72.5, 16),
        "ecmwf_ifs025": ModelDay("ecmwf_ifs025", 72.5, 71.0, 7, 72.0, 16),
    })
    return now, metar, fc


def test_state_is_semantic():
    now, metar, fc = _inputs()
    st = build_state(station=NYC, metar=metar, forecast=fc, taf=None, afd=None, now=now)
    assert st["station"].startswith("New York City — LaGuardia Airport (KLGA)")
    assert "late afternoon" in st["local_time"]
    assert "running much warmer than the models expected" in st["observations"]   # +3.5°F ≈ 1.9 °C-steps
    assert "do not expect it to get warmer" in st["models"]
    assert st["airport_forecast"] == "not available" and "forecaster_discussion" not in st


def test_trim_afd_keeps_today_sections():
    afd = (".KEY MESSAGES...\nWarm and dry.\n&&\n.SYNOPSIS...\nHigh pressure.\n&&\n"
           ".LONG TERM /MONDAY THROUGH FRIDAY/...\nlots of text\n&&\n.AVIATION...\nVFR\n&&\n")
    out = trim_afd(afd)
    assert "Warm and dry" in out and "High pressure" in out and "lots of text" not in out and "VFR" not in out


async def test_evaluate_parses_sdk_response(monkeypatch):
    sent = {}

    def handler(req):
        sent["body"] = json.loads(req.content)
        return httpx2.Response(200, headers={"x-typesafe-request-id": "req_1"}, json={
            "model": "jev-1.13.0", "usage": {"input_tokens": 800, "output_tokens": 0},
            "answers": {
                "vs_models": {"type": "choice", "choice": "warmer", "confidence": 0.55, "probabilities": {
                    "much_cooler": 0.02, "cooler": 0.08, "as_forecast": 0.3, "warmer": 0.45, "much_warmer": 0.15}},
                "peak_passed": {"type": "noul", "noul": 0.7}}})

    from typesafe_sdk import AsyncTypeSafeClient
    client = JevClient()
    client._client = AsyncTypeSafeClient(api_key="k", model="jev-1.13.0", transport=httpx2.MockTransport(handler))
    now, metar, fc = _inputs()
    state = build_state(station=NYC, metar=metar, forecast=fc, taf=None, afd=None, now=now)
    ev = await client.evaluate(state)
    await client.aclose()

    assert set(sent["body"]["questions"]) == {"vs_models", "peak_passed"}
    assert set(sent["body"]["questions"]["vs_models"]["criteria"]) == set(jevmod.JEV_SHIFT_LABELS)
    assert ev.direction == "warmer" and ev.confidence == 0.55 and ev.p_peak_passed == 0.7
    assert ev.shift_probs["warmer"] == 0.45 and ev.raw["request_id"] == "req_1"


def test_jev_labels_match_probability_shifts():
    from weatherbot.probability import JEV_SHIFT_STEPS
    assert set(jevmod.JEV_SHIFT_LABELS) == set(JEV_SHIFT_STEPS)
