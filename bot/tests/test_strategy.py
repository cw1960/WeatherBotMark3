"""Smoke tests for the strategy gate."""
from datetime import date, datetime, timezone

from weatherbot import strategy
from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.models import BotConfig, JevEvaluation, MetarObservation, ModelForecast

_STATION = to_station(BY_SLUG["nyc"])
_NOW = datetime.now(timezone.utc)


def _inputs(divergence: float, jev_dir: str = "warmer", jev_conf: float = 0.9):
    metar = MetarObservation(icao_code="KLGA", observed_at=_NOW, temp=70, high_so_far=70 + divergence, unit="F")
    fc = ModelForecast(icao_code="KLGA", model="hrrr", target_date=date.today(), high=70, unit="F")
    jev = JevEvaluation(direction=jev_dir, confidence=jev_conf, rationale="test")
    return metar, fc, jev


def _run(metar, fc, jev, price=0.40):
    return strategy.evaluate(
        station=_STATION, cfg=BotConfig(), metar=metar, forecast=fc, jev=jev,
        target_date=date.today(), outcome_label="72-73°F", token_id="t", market_price=price,
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


def test_signal_is_buy_within_limits():
    sig, reason = _run(*_inputs(3.0))
    assert reason is None and sig is not None
    assert sig.side == "BUY" and sig.size_usdc <= BotConfig().max_order_usdc
