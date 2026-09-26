"""
Bucket selection: compare our probability for each bucket with its best ask on Polymarket.

BLEND RULE (default since the intraday backtest): when the market's own view is passed in
(`market_probs`, from bid/ask mid prices), "our probability" below is the BLEND
    p ∝ ours^w · market^(1−w)          (w = BLEND_WEIGHT)
i.e. the market price is a major input and the bot only nudges it. The intraday backtest showed
this blend beats the market alone ~1 h before each city's peak, while the bot on its own does not.

    edge(bucket) = P_ours(bucket) − best_ask(bucket)        (a YES share pays $1 if right)

Trade the bucket with the largest edge when ALL of these hold:
  * edge ≥ config.min_edge_pct                      (after Jev's adjustment)
  * P_models_only(bucket) > best_ask                 (the raw models agree it's cheap — Jev
                                                       alone can't create a trade)
  * P_ours(bucket) ≥ MIN_PROB                        (don't buy far tails; the distribution's
                                                       tails are the least trustworthy part)
FOK price cap = P_ours − min_edge, rounded DOWN to the cent, so every share that fills still
carries at least the minimum edge. If the cap is below the best ask there is no trade.
"""
from __future__ import annotations

from datetime import date
from math import floor

from .exchange.markets import Bucket
from .models import BotConfig, Station, TradeSignal

MIN_PROB = 0.10
MAX_PRICE = 0.99
MIN_ASK, MAX_ASK = 0.01, 0.95   # same price range as the backtests' paper trades
BLEND_WEIGHT = 0.5              # share of OUR view in the blend (rest = market)
# Real edges in liquid markets are small. A huge gap between us and the market almost always
# means WE are missing information (a data error, a missed peak, an overconfident model).
MAX_PLAUSIBLE_EDGE = 0.30


def select_trade(
    *,
    station: Station,
    cfg: BotConfig,
    target_date: date,
    buckets: tuple[Bucket, ...] | list[Bucket],
    probs: dict[str, float],
    probs_models_only: dict[str, float],
    asks: dict[str, float],
    divergence: float,
    market_probs: dict[str, float] | None = None,
    blend_weight: float = BLEND_WEIGHT,
) -> tuple[TradeSignal | None, str, list[dict]]:
    """Returns (signal or None, reason, per-bucket table for the audit log)."""
    min_edge = cfg.min_edge_pct / 100
    ours = probs
    if market_probs:
        probs = blend(probs, market_probs, blend_weight)
        probs_models_only = blend(probs_models_only, market_probs, blend_weight)
    table, candidates = [], []
    for b in buckets:
        p, p0, ask = probs.get(b.label, 0.0), probs_models_only.get(b.label, 0.0), asks.get(b.label)
        edge = (p - ask) if ask is not None else None
        row = {"bucket": b.label, "p": round(p, 4), "p_models_only": round(p0, 4),
               "ask": ask, "edge": None if edge is None else round(edge, 4)}
        if market_probs:
            row.update(p_bot=round(ours.get(b.label, 0.0), 4),
                       p_market=round(market_probs.get(b.label, 0.0), 4))
        table.append(row)
        if ask is None or b.closed or not (MIN_ASK <= ask <= MAX_ASK):
            continue
        if edge >= min_edge and p0 > ask and p >= MIN_PROB and edge <= MAX_PLAUSIBLE_EDGE:
            candidates.append((edge, b, p, ask))

    if not candidates:
        best = max((r for r in table if r["edge"] is not None), key=lambda r: r["edge"], default=None)
        if best is None:
            return None, "no prices on any bucket", table
        if best["edge"] > MAX_PLAUSIBLE_EDGE:
            return None, (f"edge {best['edge']*100:+.0f}% on {best['bucket']} is implausibly large "
                          f"— assuming our data is wrong, not trading"), table
        return None, (f"best edge {best['edge']*100:+.1f}% on {best['bucket']} "
                      f"(p={best['p']:.2f} vs ask {best['ask']:.2f}) — below {cfg.min_edge_pct}% "
                      f"or not confirmed by models"), table

    edge, bucket, p, ask = max(candidates, key=lambda c: c[0])
    cap = min(MAX_PRICE, floor(round((p - min_edge) * 100, 6)) / 100)
    if cap < ask:
        return None, f"price cap {cap:.2f} below ask {ask:.2f} on {bucket.label}", table

    signal = TradeSignal(
        station=station,
        target_date=target_date,
        outcome_label=bucket.label,
        token_id=bucket.token_yes,
        side="BUY",
        price=cap,
        size_usdc=min(cfg.max_order_usdc, cfg.max_budget_usdc),
        market_price=ask,
        implied_probability=ask,
        model_probability=round(p, 4),
        edge_pct=round(edge * 100, 2),
        divergence=divergence,
    )
    return signal, "ok", table


def blend(ours: dict[str, float], mkt: dict[str, float], w: float) -> dict[str, float]:
    """Geometric blend p ∝ ours^w · market^(1−w), renormalised over all buckets."""
    out = {k: (max(ours.get(k, 0.0), 1e-4) ** w) * (max(mkt.get(k, 0.0), 1e-4) ** (1 - w))
           for k in set(ours) | set(mkt)}
    tot = sum(out.values())
    return {k: v / tot for k, v in out.items()}


def market_probabilities(mids: dict[str, float], n_buckets: int) -> dict[str, float] | None:
    """The market's probabilities from mid prices, normalised to sum to 1. None when more than one
    bucket has no price (then the market's view is incomplete and we don't trade)."""
    mids = {k: v for k, v in mids.items() if v is not None and v > 0}
    if len(mids) < n_buckets - 1 or sum(mids.values()) <= 0:
        return None
    tot = sum(mids.values())
    return {k: v / tot for k, v in mids.items()}
