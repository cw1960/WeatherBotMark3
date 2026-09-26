"""
Bucket probabilities — all the maths lives here, in code (TypeSafe's guidance: "Jev is not
a calculator"). Jev contributes two semantic judgments, which this module turns into numbers.

Model of the day's final high, per forecast model m:
    X_m ~ Normal(mean_m, sigma)
    mean_m = model's max over the hours still to come today
           + ALPHA × (observed high so far − model's max over hours already past)   # bias persistence
           + Jev shift                                                              # judgment
    final  = max(observed high so far, X_m)       # the high can only go up
Models are mixed with equal weight. With probability p_peak_passed (Jev) no further heating
happens. The high recorded so far is itself slightly uncertain (the true peak may sit a bit
above the reports we saw), so the floor is observed + a small upward-only error. Nothing is
ever treated as certain. The market settles on the whole-degree value (round half up), so
bucket probability = probability mass of the rounded final.

sigma grows with the hours of heating left: little can change at 5 pm, a lot at 8 am.
All constants below are first guesses to be tuned by backtesting against settled markets.
"""
from __future__ import annotations

from math import erf, floor, sqrt
from typing import Iterable

from .exchange.markets import Bucket

# --- tunables (°C; converted for °F stations) ------------------------------------------
ALPHA_BIAS = 0.5          # share of today's observed-vs-model bias expected to persist
MIN_ELAPSED_FOR_BIAS = 3  # need this many hours of the local day behind us before trusting bias
SIGMA_FLOOR_C = 0.6       # uncertainty with ~no heating left
SIGMA_RANGE_C = 1.0       # extra uncertainty with a full day of heating ahead
JEV_STEP_C = 1.0          # one "warmer"/"cooler" step, in °C
JEV_SHIFT_STEPS = {"much_cooler": -2, "cooler": -1, "as_forecast": 0, "warmer": 1, "much_warmer": 2}


def _unit_scale(unit: str) -> float:
    return 1.8 if unit == "F" else 1.0


def _phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


OBS_GAP_SIGMA_C = 0.2   # small allowance that the settled reading differs from what we saw


def sigma(hours_remaining: int, unit: str, sigma_full: float | None = None) -> float:
    """Uncertainty of further heating. `sigma_full` (station unit) is the learned full-day value
    for this station (calibration.py); without it the generic default is used."""
    frac = min(max(hours_remaining, 0), 12) / 12
    full = sigma_full if sigma_full is not None else (SIGMA_FLOOR_C + SIGMA_RANGE_C) * _unit_scale(unit)
    floor_ = min(SIGMA_FLOOR_C * _unit_scale(unit), 0.6 * full)
    return floor_ + (full - floor_) * frac


def _rounded(v: float) -> int:
    return int(floor(v + 0.5))


def model_mean(
    *, remaining_high: float | None, elapsed_high: float | None, observed_high: float | None,
    hours_elapsed: int, unit: str, shift_steps: float = 0.0,
) -> float | None:
    if remaining_high is None:
        return None
    mean = remaining_high
    if observed_high is not None and elapsed_high is not None and hours_elapsed >= MIN_ELAPSED_FOR_BIAS:
        mean += ALPHA_BIAS * (observed_high - elapsed_high)
    return mean + shift_steps * JEV_STEP_C * _unit_scale(unit)


def distribution(
    *, observed_high: float | None, means: Iterable[float], hours_remaining: int, unit: str,
    p_peak_passed: float = 0.0, sigma_full: float | None = None,
    weights: Iterable[float] | None = None,
) -> dict[int, float]:
    """Probability of each whole-degree settlement value.

    final = max(F, X), independent:
      F = observed high + |N(0, OBS_GAP)|   — the true peak so far may sit a little above what
                                             the reports we saw captured (never below it)
      X = further heating: weighted mixture of N(mean_m, sigma) (weights = each model's
          track record at this station; equal if uncalibrated); with probability
          p_peak_passed (or when no hours remain) there is no further heating.
    CDF_final(x) = CDF_F(x) · CDF_X(x), and P(k) = CDF(k+0.5) − CDF(k−0.5).
    """
    means = list(means)
    w = list(weights) if weights is not None else [1.0] * len(means)
    wsum = sum(w) or 1.0
    w = [x / wsum for x in w]
    gap = OBS_GAP_SIGMA_C * _unit_scale(unit)
    s = sigma(hours_remaining, unit, sigma_full)
    heating = bool(means) and hours_remaining > 0
    p_none = 1.0 if not heating else min(max(p_peak_passed, 0.0), 1.0)

    def cdf_f(x: float) -> float:
        if observed_high is None:
            return 1.0
        if x < observed_high:
            return 0.0
        return erf((x - observed_high) / (gap * sqrt(2.0)))

    def cdf_x(x: float) -> float:
        if not heating:
            return 1.0
        mix = sum(wi * _phi((x - mu) / s) for wi, mu in zip(w, means))
        return p_none + (1.0 - p_none) * mix

    if observed_high is None and not heating:
        return {}
    lows = ([observed_high] if observed_high is not None else []) + ([min(means) - 6 * s] if heating else [])
    highs = ([observed_high + 6 * gap] if observed_high is not None else []) + ([max(means) + 6 * s] if heating else [])
    k_lo = _rounded(max(lows) if observed_high is not None else min(lows))
    k_hi = _rounded(max(highs))
    out: dict[int, float] = {}
    for k in range(k_lo, k_hi + 1):
        p = cdf_f(k + 0.5) * cdf_x(k + 0.5) - cdf_f(k - 0.5) * cdf_x(k - 0.5)
        if p > 1e-9:
            out[k] = p
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total else out


def bucket_probabilities(dist: dict[int, float], buckets: Iterable[Bucket]) -> dict[str, float]:
    probs: dict[str, float] = {}
    for b in buckets:
        probs[b.label] = sum(p for k, p in dist.items()
                             if (b.lower is None or k >= b.lower) and (b.upper is None or k <= b.upper))
    return probs


def forecast_distribution(
    forecast, observed_high: float | None, unit: str,
    jev_shift_probs: dict[str, float] | None = None, p_peak_passed: float = 0.0,
    cal=None,
) -> dict[int, float]:
    """Full pipeline for one city: models (+ station calibration + bias persistence) × Jev shift
    mixture → settlement value distribution. `forecast` is a weather.forecast.StationForecast;
    `cal` is an optional calibration.StationCal learned by backtest.py."""
    station_bias = cal.bias if cal is not None else {}
    station_w = (cal.weights or {}) if cal is not None else {}
    sigma_full = cal.sigma if cal is not None else None
    shift_probs = jev_shift_probs or {"as_forecast": 1.0}
    models = list(forecast.models.values())
    hours_remaining = max((m.hours_remaining for m in models), default=0)
    combined: dict[int, float] = {}
    for cat, p_cat in shift_probs.items():
        if p_cat <= 0:
            continue
        steps = JEV_SHIFT_STEPS.get(cat, 0)
        means, weights = [], []
        default_w = (sum(station_w.values()) / len(station_w)) if station_w else 1.0
        for m in models:
            b = station_bias.get(m.model, 0.0)
            mu = model_mean(
                remaining_high=None if m.remaining_high is None else m.remaining_high + b,
                elapsed_high=None if m.elapsed_high is None else m.elapsed_high + b,
                observed_high=observed_high, hours_elapsed=m.hours_elapsed,
                unit=unit, shift_steps=steps)
            if mu is not None:
                means.append(mu)
                weights.append(station_w.get(m.model, default_w))
        for k, p in distribution(observed_high=observed_high, means=means,
                                 hours_remaining=hours_remaining, unit=unit,
                                 p_peak_passed=p_peak_passed, sigma_full=sigma_full,
                                 weights=weights).items():
            combined[k] = combined.get(k, 0.0) + p_cat * p
    total = sum(combined.values())
    return {k: v / total for k, v in combined.items()} if total else combined
