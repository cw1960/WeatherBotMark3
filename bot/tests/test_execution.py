"""Execution safety: FOK only, and nothing touches the API when trading is off."""
import ast
from datetime import date
from pathlib import Path

from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.exchange import polymarket as pm
from weatherbot.models import TradeSignal

ROOT = Path(__file__).resolve().parents[1]


def test_order_type_constant_is_fok():
    assert pm.ORDER_TYPE == "FOK"


def test_no_gtc_or_limit_orders_anywhere_in_bot():
    for path in (ROOT / "weatherbot").rglob("*.py"):
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "place_limit_order":
                raise AssertionError(f"place_limit_order used in {path} (rests on book like GTC)")
            if isinstance(node, ast.Constant) and node.value in ("GTC", "GTD", "FAK"):
                raise AssertionError(f"forbidden order type {node.value!r} in {path}")


async def test_simulation_never_creates_a_secure_client(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("secure client must not be created when live=False")
    monkeypatch.setattr(pm.AsyncSecureClient, "create", boom)
    ex = pm.PolymarketExecutor()
    sig = TradeSignal(station=to_station(BY_SLUG["nyc"]), target_date=date.today(), outcome_label="72-73°F",
                      token_id="t", side="BUY", price=0.4, size_usdc=5, market_price=0.4,
                      implied_probability=0.4, model_probability=0.6, edge_pct=20, divergence=3)
    res = await ex.place_fok(sig, live=False)
    assert res.status == "SIMULATED" and res.raw["order_type"] == "FOK"


class _FakeSecure:
    """Stands in for AsyncSecureClient; records the call and returns a real SDK response model."""
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    async def place_market_order(self, **kw):
        self.calls.append(kw)
        return self.resp

    async def close(self):
        pass


def _signal():
    return TradeSignal(station=to_station(BY_SLUG["nyc"]), target_date=date.today(), outcome_label="72-73°F",
                       token_id="tok", side="BUY", price=0.4, size_usdc=10, market_price=0.4,
                       implied_probability=0.4, model_probability=0.6, edge_pct=20, divergence=3)


async def _live(resp):
    from decimal import Decimal  # noqa: F401
    ex = pm.PolymarketExecutor()
    ex._secure = _FakeSecure(resp)
    return await ex.place_fok(_signal(), live=True), ex._secure.calls


async def test_live_fill_uses_fok_with_price_cap():
    from decimal import Decimal
    from polymarket.models.clob.order_response import AcceptedOrder
    resp = AcceptedOrder(order_id="0xabc", status="matched", making_amount=Decimal("10"),
                         taking_amount=Decimal("25"), trade_ids=("t1",), transactions_hashes=())
    res, calls = await _live(resp)
    assert calls == [{"token_id": "tok", "side": "BUY", "amount": Decimal("10"),
                      "max_price": Decimal("0.4"), "order_type": "FOK"}]
    assert res.status == "FILLED" and res.fill_size == 25 and res.fill_price == 0.4


async def test_live_fok_not_filled_is_killed():
    from polymarket.models.clob.order_response import RejectedOrder
    res, _ = await _live(RejectedOrder(code="fok_not_filled", message="order couldn't be fully filled"))
    assert res.status == "KILLED"


async def test_live_other_rejection():
    from polymarket.models.clob.order_response import RejectedOrder
    res, _ = await _live(RejectedOrder(code="not_enough_balance", message="no money"))
    assert res.status == "REJECTED" and "not_enough_balance" in res.error_message


async def test_live_delayed_is_flagged():
    from decimal import Decimal
    from polymarket.models.clob.order_response import AcceptedOrder
    resp = AcceptedOrder(order_id="0xabc", status="delayed", making_amount=Decimal("0"),
                         taking_amount=Decimal("0"), trade_ids=(), transactions_hashes=())
    res, _ = await _live(resp)
    assert res.status == "ERROR" and "delayed" in res.error_message
