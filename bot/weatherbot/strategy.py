"""
Divergence strategy: METAR ground truth vs HRRR model, gated by Jev's AFD read.

This is deliberately thin — it returns a TradeSignal or a skip reason, and the
main loop handles the trading_active gate and audit logging.
"""
from __future__ import annotations

from datetime import date

from .models import BotConfig, HrrrForecast, JevEvaluation, MetarObservation, Station, TradeSignal


def evaluate(
    *,
    station: Station,
    cfg: BotConfig,
    metar: MetarObservation,
    hrrr: HrrrForecast,
    jev: JevEvaluation,
    target_date: date,
    outcome_label: str,
    token_id: str,
    market_price: float,
) -> tuple[TradeSignal | None, str | None]:
    divergence = round(metar.high_so_far_f - hrrr.high_f, 1)

    if abs(divergence) < cfg.min_divergence_deg:
        return None, f"divergence {divergence:+.1f}°F below threshold {cfg.min_divergence_deg}"

    if jev.confidence < cfg.min_jev_confidence:
        return None, f"jev confidence {jev.confidence:.2f} below {cfg.min_jev_confidence}"

    # Jev must agree with the direction the observation is already pushing.
    obs_direction = "warmer" if divergence > 0 else "cooler"
    if jev.direction not in (obs_direction, "neutral"):
        return None, f"jev says {jev.direction}, observation says {obs_direction}"

    # TODO: real probability model. Placeholder: blend divergence + Jev confidence.
    model_probability = min(0.95, 0.5 + 0.1 * abs(divergence) * jev.confidence)
    implied_probability = market_price
    edge_pct = round((model_probability - implied_probability) * 100, 2)

    if edge_pct < cfg.min_edge_pct:
        return None, f"edge {edge_pct:.1f}% below {cfg.min_edge_pct}%"

    signal = TradeSignal(
        station=station,
        target_date=target_date,
        outcome_label=outcome_label,
        token_id=token_id,
        side="BUY",
        price=round(market_price, 2),
        size_usdc=min(cfg.max_order_usdc, cfg.max_budget_usdc),
        market_price=market_price,
        implied_probability=implied_probability,
        model_probability=model_probability,
        edge_pct=edge_pct,
        divergence_f=divergence,
    )
    return signal, None
