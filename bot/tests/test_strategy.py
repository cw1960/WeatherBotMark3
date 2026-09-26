"""Smoke tests for the strategy gate — run with `pytest` from /bot."""
from datetime import date, datetime, timezone
from uuid import uuid4

from weatherbot import strategy
from weatherbot.models import BotConfig, HrrrForecast, JevEvaluation, MetarObservation, Station

_STATION = Station(
    id=uuid4(), city_label="New York City", icao_code="KNYC", nws_office="OKX",
    polymarket_market_id="m", polymarket_condition_id="0xabc",
)
_NOW = datetime.now(timezone.utc)


def _inputs(divergence: float, jev_dir: str = "warmer", jev_conf: float = 0.9):
    metar = MetarObservation(icao_code="KNYC", observed_at=_NOW, temp_f=70, high_so_far_f=70 + divergence)
    hrrr = HrrrForecast(icao_code="KNYC", run_time=_NOW, target_date=date.today(), high_f=70)
    jev = JevEvaluation(direction=jev_dir, confidence=jev_conf, rationale="test")
    return metar, hrrr, jev


def _run(metar, hrrr, jev, price=0.40):
    return strategy.evaluate(
        station=_STATION, cfg=BotConfig(), metar=metar, hrrr=hrrr, jev=jev,
        target_date=date.today(), outcome_label="72-73", token_id="t", market_price=price,
    )


def test_small_divergence_skips():
    sig, reason = _run(*_inputs(1.0))
    assert sig is None and "divergence" in reason


def test_low_jev_confidence_skips():
    sig, reason = _run(*_inputs(3.0, jev_conf=0.3))
    assert sig is None and "confidence" in reason


def test_jev_disagreement_skips():
    sig, reason = _run(*_inputs(3.0, jev_dir="cooler"))
    assert sig is None and "jev says cooler" in reason


def test_signal_is_fok_buy():
    sig, reason = _run(*_inputs(3.0))
    assert reason is None and sig is not None
    assert sig.side == "BUY" and sig.size_usdc <= BotConfig().max_order_usdc
