"""Typed records shared across modules (mirror the Supabase schema)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Station(BaseModel):
    id: UUID
    city_label: str
    icao_code: str
    nws_office: str | None = None
    nws_gridpoint: str | None = None
    timezone: str = "America/New_York"
    polymarket_market_id: str
    polymarket_condition_id: str
    polymarket_slug: str | None = None
    unit: Literal["F", "C"] = "F"
    is_active: bool = True


class BotConfig(BaseModel):
    trading_active: bool = False
    max_budget_usdc: float = 100.0
    max_order_usdc: float = 25.0
    daily_loss_limit_usdc: float = 50.0
    min_divergence_deg: float = 2.0
    min_edge_pct: float = 8.0
    min_jev_confidence: float = 0.7
    alert_divergence_deg: float = 3.0
    alert_drawdown_pct: float = 20.0
    poll_interval_seconds: int = 60


class MetarObservation(BaseModel):
    icao_code: str
    observed_at: datetime
    temp_f: float
    high_so_far_f: float
    raw: str | None = None


class HrrrForecast(BaseModel):
    icao_code: str
    run_time: datetime
    target_date: date
    high_f: float


class JevEvaluation(BaseModel):
    """Structured output requested from Jev System One for an Area Forecast Discussion."""
    direction: Literal["warmer", "cooler", "neutral", "uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    afd_issued_at: datetime | None = None
    afd_excerpt: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TradeSignal(BaseModel):
    station: Station
    target_date: date
    outcome_label: str
    token_id: str
    side: Literal["BUY", "SELL"]
    price: float
    size_usdc: float
    market_price: float
    implied_probability: float
    model_probability: float
    edge_pct: float
    divergence_f: float


class ExecutionResult(BaseModel):
    order_id: str | None = None
    status: Literal["FILLED", "KILLED", "REJECTED", "SIMULATED", "ERROR"]
    fill_size: float | None = None
    fill_price: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
