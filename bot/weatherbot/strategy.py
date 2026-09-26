"""
Bucket selection: compare our probability for each bucket with its best ask on Polymarket.

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
) -> tuple[TradeSignal | None, str, list[dict]]:
    """Returns (signal or None, reason, per-bucket table for the audit log)."""
    min_edge = cfg.min_edge_pct / 100
    table, candidates = [], []
    for b in buckets:
        p, p0, ask = probs.get(b.label, 0.0), probs_models_only.get(b.label, 0.0), asks.get(b.label)
        edge = (p - ask) if ask is not None else None
        table.append({"bucket": b.label, "p": round(p, 4), "p_models_only": round(p0, 4),
                      "ask": ask, "edge": None if edge is None else round(edge, 4)})
        if ask is None or b.closed:
            continue
        if edge >= min_edge and p0 > ask and p >= MIN_PROB:
            candidates.append((edge, b, p, ask))

    if not candidates:
        best = max((r for r in table if r["edge"] is not None), key=lambda r: r["edge"], default=None)
        if best is None:
            return None, "no prices on any bucket", table
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
