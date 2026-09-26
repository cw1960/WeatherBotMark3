"""Bucket selection: edge threshold, model confirmation, price cap."""
from datetime import date

from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.exchange.markets import Bucket
from weatherbot.models import BotConfig
from weatherbot.strategy import select_trade

ST = to_station(BY_SLUG["tokyo"])
B = [Bucket("tokyo", date(2026, 9, 26), "e", "s", str(i), f"0x{i}", lab, lo, hi, "C", f"yes{i}", f"no{i}", True, True, False)
     for i, (lab, lo, hi) in enumerate([("24°C or below", None, 24), ("25°C", 25, 25), ("26°C", 26, 26), ("27°C or higher", 27, None)])]
CFG = BotConfig(min_edge_pct=8, max_order_usdc=25, max_budget_usdc=100)


def _run(probs, probs0, asks):
    return select_trade(station=ST, cfg=CFG, target_date=date(2026, 9, 26), buckets=B,
                        probs=probs, probs_models_only=probs0, asks=asks, divergence=1.0)


def test_picks_largest_edge_and_caps_price():
    probs = {"24°C or below": 0.05, "25°C": 0.20, "26°C": 0.55, "27°C or higher": 0.20}
    asks = {"24°C or below": 0.02, "25°C": 0.30, "26°C": 0.35, "27°C or higher": 0.15}
    sig, reason, table = _run(probs, probs, asks)
    assert reason == "ok" and sig.outcome_label == "26°C" and sig.token_id == "yes2"
    assert sig.edge_pct == 20.0
    assert sig.price == 0.47            # 0.55 − 0.08, every filled share keeps ≥ 8% edge
    assert len(table) == 4


def test_edge_below_threshold_skips():
    probs = {"25°C": 0.40}
    sig, reason, _ = _run(probs, probs, {"25°C": 0.35})
    assert sig is None and "below 8" in reason


def test_jev_alone_cannot_create_a_trade():
    probs = {"26°C": 0.60}                 # Jev-adjusted: big edge
    probs0 = {"26°C": 0.30}                # models alone: not cheap
    sig, reason, _ = _run(probs, probs0, {"26°C": 0.35})
    assert sig is None and "not confirmed by models" in reason


def test_far_tail_not_bought():
    probs = {"27°C or higher": 0.09}
    sig, _, _ = _run(probs, probs, {"27°C or higher": 0.001})
    assert sig is None


def test_no_prices():
    sig, reason, _ = _run({"26°C": 0.9}, {"26°C": 0.9}, {})
    assert sig is None and reason == "no prices on any bucket"


def test_implausibly_large_edge_is_refused():
    probs = {"26°C": 0.95}
    sig, reason, _ = _run(probs, probs, {"26°C": 0.02})
    assert sig is None and "implausibly large" in reason
