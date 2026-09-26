"""Typed records shared across modules (mirror the Supabase schema).

Temperatures are always in the station's own market unit ('F' or 'C').
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Unit = Literal["F", "C"]


class Station(BaseModel):
    id: UUID
    city_slug: str
    city_label: str
    icao_code: str
    airport_name: str | None = None
    rules_phrase: str | None = None
    unit: Unit = "F"
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: int | None = None
    country: str | None = None
    resolution_source: Literal["noaa", "wunderground", "hko"] = "noaa"
    nws_office: str | None = None
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
    temp: float              # latest reading, station unit
    high_so_far: float       # max since local midnight, station unit
    unit: Unit
    n_obs_today: int = 0
    raw: str | None = None
    six_hour_max: float | None = None   # US synoptic 6-h max group (context only, not settlement)


class ModelForecast(BaseModel):
    icao_code: str
    model: str               # e.g. 'hrrr', 'ecmwf_ifs025'
    run_time: datetime | None = None
    target_date: date
    high: float              # forecast daily max, station unit
    unit: Unit


class JevEvaluation(BaseModel):
    """Jev System One's verdict on one city's evidence bundle."""
    direction: Literal["warmer", "cooler", "neutral", "uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    expected_high: float | None = None   # mean of the final distribution (computed in code)
    shift_probs: dict[str, float] = Field(default_factory=dict)  # vs_models Choice probabilities
    p_peak_passed: float = 0.0                                    # peak_passed Noul probability
    afd_issued_at: datetime | None = None
    afd_excerpt: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TradeSignal(BaseModel):
    station: Station
    target_date: date
    outcome_label: str
    token_id: str
    side: Literal["BUY", "SELL"]
    price: float             # FOK price cap (max price for BUY)
    size_usdc: float
    market_price: float
    implied_probability: float
    model_probability: float
    edge_pct: float
    divergence: float


class ExecutionResult(BaseModel):
    order_id: str | None = None
    status: Literal["FILLED", "KILLED", "REJECTED", "SIMULATED", "ERROR"]
    fill_size: float | None = None
    fill_price: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
