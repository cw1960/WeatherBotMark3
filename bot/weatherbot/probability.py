"""
Bucket probabilities — all the maths lives here, in code (TypeSafe's guidance: "Jev is not
a calculator"). Jev contributes two semantic judgments, which this module turns into numbers.

Model of the day's final high, per forecast model m:
    X_m ~ Normal(mean_m, sigma)
    mean_m = model's max over the hours still to come today
           + ALPHA × (observed high so far − model's max over hours already past)   # bias persistence
           + Jev shift                                                              # judgment
    final  = max(observed high so far, X_m)       # the high can only go up
Models are mixed with equal weight. With probability p_peak_passed (Jev) the high is
already in, so final = observed high. The market settles on the whole-degree value
(round half up), so bucket probability = probability mass of the rounded final.

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


def sigma(hours_remaining: int, unit: str) -> float:
    frac = min(max(hours_remaining, 0), 12) / 12
    return (SIGMA_FLOOR_C + SIGMA_RANGE_C * frac) * _unit_scale(unit)


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
    p_peak_passed: float = 0.0,
) -> dict[int, float]:
    """Probability of each whole-degree settlement value."""
    means = list(means)
    out: dict[int, float] = {}
    obs_r = _rounded(observed_high) if observed_high is not None else None

    def add(k: int, p: float) -> None:
        if p > 0:
            out[k] = out.get(k, 0.0) + p

    if obs_r is not None and p_peak_passed > 0:
        add(obs_r, p_peak_passed)
    rest = 1.0 - (p_peak_passed if obs_r is not None else 0.0)
    if not means or hours_remaining <= 0:
        if obs_r is not None:
            add(obs_r, rest)
        return out

    s = sigma(hours_remaining, unit)
    w = rest / len(means)
    for mu in means:
        lo_k = _rounded(mu - 6 * s)
        hi_k = _rounded(mu + 6 * s)
        if obs_r is not None:
            # everything at or below the observed high settles on the observed high
            add(obs_r, w * _phi((obs_r + 0.5 - mu) / s))
            lo_k = max(lo_k, obs_r + 1)
        for k in range(lo_k, hi_k + 1):
            add(k, w * (_phi((k + 0.5 - mu) / s) - _phi((k - 0.5 - mu) / s)))
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
) -> dict[int, float]:
    """Full pipeline for one city: models (+ bias persistence) × Jev shift mixture → settlement
    value distribution. `forecast` is a weather.forecast.StationForecast."""
    shift_probs = jev_shift_probs or {"as_forecast": 1.0}
    models = list(forecast.models.values())
    hours_remaining = max((m.hours_remaining for m in models), default=0)
    combined: dict[int, float] = {}
    for cat, p_cat in shift_probs.items():
        if p_cat <= 0:
            continue
        steps = JEV_SHIFT_STEPS.get(cat, 0)
        means = [
            mu for m in models
            if (mu := model_mean(remaining_high=m.remaining_high, elapsed_high=m.elapsed_high,
                                 observed_high=observed_high, hours_elapsed=m.hours_elapsed,
                                 unit=unit, shift_steps=steps)) is not None
        ]
        for k, p in distribution(observed_high=observed_high, means=means,
                                 hours_remaining=hours_remaining, unit=unit,
                                 p_peak_passed=p_peak_passed).items():
            combined[k] = combined.get(k, 0.0) + p_cat * p
    total = sum(combined.values())
    return {k: v / total for k, v in combined.items()} if total else combined
