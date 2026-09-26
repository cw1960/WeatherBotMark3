"""
Polymarket execution via the modern `polymarket-client` SDK.

Hard rules enforced here, not left to callers:
  * Every order is OrderType.FOK. There is no code path that produces GTC.
  * Credentials come from settings (os.environ) only.
  * `trading_active` is re-read by the caller immediately before `place_fok()`;
    this module additionally refuses to place anything if `live` is False.

NOTE: method names on AsyncSecureClient (create_order / post_order / get_order_book)
should be checked against the installed SDK version; they are marked below.
"""
from __future__ import annotations

from typing import Any

import structlog

from polymarket import AsyncSecureClient  # modern SDK — NOT py-clob-client
from polymarket.types import OrderType, Side  # TODO: confirm import path in installed version

from ..models import ExecutionResult, TradeSignal
from ..settings import settings

log = structlog.get_logger(__name__)


class PolymarketExecutor:
    def __init__(self) -> None:
        self._client: AsyncSecureClient | None = None

    async def __aenter__(self) -> "PolymarketExecutor":
        self._client = AsyncSecureClient(  # TODO: confirm constructor kwargs
            private_key=settings.pm_private_key,
            chain_id=settings.pm_chain_id,
            funder=settings.pm_funder_address or None,
            api_key=settings.pm_api_key or None,
            api_secret=settings.pm_api_secret or None,
            api_passphrase=settings.pm_api_passphrase or None,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.close()  # TODO: confirm

    # ------------------------------------------------------------- market data
    async def best_ask(self, token_id: str) -> float | None:
        assert self._client is not None
        book = await self._client.get_order_book(token_id)  # TODO: confirm
        asks = getattr(book, "asks", None) or book.get("asks", [])
        if not asks:
            return None
        return float(min(float(a.price if hasattr(a, "price") else a["price"]) for a in asks))

    # --------------------------------------------------------------- execution
    async def place_fok(self, signal: TradeSignal, *, live: bool) -> ExecutionResult:
        """
        Fill-Or-Kill limit order. `live` must be the freshly-read config.trading_active.
        When live is False we return a SIMULATED result and never touch the API.
        """
        if not live:
            log.info("simulated_fok", token=signal.token_id, side=signal.side,
                     price=signal.price, size_usdc=signal.size_usdc)
            return ExecutionResult(status="SIMULATED", fill_size=None, fill_price=None,
                                   raw={"simulated": True, "signal": signal.model_dump(mode="json")})

        assert self._client is not None
        try:
            order = await self._client.create_order(  # TODO: confirm signature
                token_id=signal.token_id,
                side=Side.BUY if signal.side == "BUY" else Side.SELL,
                price=signal.price,
                size=round(signal.size_usdc / signal.price, 2),  # shares
            )
            resp = await self._client.post_order(order, order_type=OrderType.FOK)  # FOK, always
            status = str(getattr(resp, "status", resp.get("status", ""))).upper()
            mapped = "FILLED" if status in {"MATCHED", "FILLED"} else "KILLED"
            return ExecutionResult(
                order_id=getattr(resp, "order_id", None) or resp.get("orderID"),
                status=mapped,
                fill_size=getattr(resp, "size_matched", None),
                fill_price=signal.price if mapped == "FILLED" else None,
                raw=resp if isinstance(resp, dict) else getattr(resp, "__dict__", {}),
            )
        except Exception as exc:
            log.error("fok_error", error=str(exc), token=signal.token_id)
            return ExecutionResult(status="ERROR", error_message=str(exc))
