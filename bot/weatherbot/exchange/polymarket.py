"""
Polymarket execution via the official `polymarket-client` SDK (v0.11, `import polymarket`).

Checked against the installed SDK source (2026-09-26):
  * Order books come from AsyncPublicClient.get_order_book(token_id=...) — no auth needed,
    so simulation mode never needs a private key. Asks are sorted DESCENDING (best ask last).
  * A Fill-Or-Kill order with a price cap is
        AsyncSecureClient.place_market_order(token_id=, side="BUY", amount=<USDC>,
                                             max_price=<cap>, order_type="FOK")
    `order_type` defaults to "FAK" in the SDK, so we ALWAYS pass ORDER_TYPE explicitly.
    (`place_limit_order` has no order-type option — it rests on the book like GTC — so it is
    never used here.)
  * The response is AcceptedOrder (ok=True, status 'matched' | 'live' | 'delayed') or
    RejectedOrder (ok=False, code e.g. 'fok_not_filled').

Hard rules enforced here, not left to callers:
  * ORDER_TYPE is the constant "FOK". There is no code path that sends anything else.
  * Credentials come from settings (os.environ) only.
  * The caller re-reads config.trading_active immediately before place_fok();
    this module also refuses to touch the API unless live=True.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

import structlog
from polymarket import AsyncPublicClient, AsyncSecureClient

from ..models import ExecutionResult, TradeSignal
from ..settings import settings

log = structlog.get_logger(__name__)

ORDER_TYPE: Final = "FOK"


class PolymarketExecutor:
    def __init__(self) -> None:
        self._public: AsyncPublicClient | None = None
        self._secure: AsyncSecureClient | None = None

    async def __aenter__(self) -> "PolymarketExecutor":
        self._public = AsyncPublicClient()
        await self._public.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._secure is not None:
            await self._secure.close()
        if self._public is not None:
            await self._public.close()

    async def _secure_client(self) -> AsyncSecureClient:
        """Created lazily, only the first time a LIVE order is sent."""
        if self._secure is None:
            settings.require("pm_private_key")
            self._secure = await AsyncSecureClient.create(
                private_key=settings.pm_private_key,
                wallet=settings.pm_wallet_address or None,
            )
        return self._secure

    # ------------------------------------------------------------- market data
    async def best_ask(self, token_id: str) -> tuple[float, float] | None:
        """(best ask price, size available at it) for a token, or None if the book is empty."""
        assert self._public is not None, "use `async with PolymarketExecutor()`"
        book = await self._public.get_order_book(token_id=token_id)
        if not book.asks:
            return None
        best = min(book.asks, key=lambda lvl: lvl.price)  # don't rely on sort order
        return float(best.price), float(best.size)

    # --------------------------------------------------------------- execution
    async def place_fok(self, signal: TradeSignal, *, live: bool) -> ExecutionResult:
        """Fill-Or-Kill BUY capped at signal.price. `live` must be the freshly-read
        config.trading_active; when False we return SIMULATED and never call the API."""
        if not live:
            log.info("simulated_fok", token=signal.token_id, price_cap=signal.price,
                     size_usdc=signal.size_usdc, city=signal.station.city_slug)
            return ExecutionResult(status="SIMULATED",
                                   raw={"simulated": True, "order_type": ORDER_TYPE,
                                        "signal": signal.model_dump(mode="json")})

        if signal.side != "BUY":
            return ExecutionResult(status="REJECTED", error_message="only BUY is implemented")

        try:
            client = await self._secure_client()
            resp = await client.place_market_order(
                token_id=signal.token_id,
                side="BUY",
                amount=Decimal(str(signal.size_usdc)),
                max_price=Decimal(str(signal.price)),
                order_type=ORDER_TYPE,
            )
        except Exception as exc:
            log.error("fok_error", error=str(exc), token=signal.token_id)
            return ExecutionResult(status="ERROR", error_message=str(exc))

        raw = resp.model_dump(mode="json")
        if not resp.ok:
            status = "KILLED" if resp.code == "fok_not_filled" else "REJECTED"
            return ExecutionResult(status=status, raw=raw, error_message=f"{resp.code}: {resp.message}")

        # BUY: making_amount = USDC paid, taking_amount = shares received.
        shares = float(resp.taking_amount)
        paid = float(resp.making_amount)
        if resp.status == "delayed":
            # Matching engine queued it; outcome unknown right now. Flag for manual check.
            return ExecutionResult(order_id=resp.order_id, status="ERROR", raw=raw,
                                   error_message="order delayed by exchange — verify fill manually")
        filled = resp.status == "matched" and shares > 0
        return ExecutionResult(
            order_id=resp.order_id,
            status="FILLED" if filled else "KILLED",
            fill_size=shares or None,
            fill_price=(paid / shares) if shares else None,
            raw=raw,
        )
