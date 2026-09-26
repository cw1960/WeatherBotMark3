"""
Per-station calibration learned from history (see backtest.py), used by probability.py.

Forecast models predict the average over a grid box several km wide, not one airport
thermometer, so each station runs systematically warmer or cooler than each model. For every
station we learn:
  * bias[model]  — median of (settled high − model's forecast high), shrunk toward 0 when few
                   days are available
  * weights      — how much to trust each model here (inverse error after bias correction)
  * sigma        — the uncertainty that would have scored best on past days (grid search on the
                   actual bucket log loss), lightly pulled toward the default with few days

Stored in bot/calibration/station_calibration.json (committed; not secret). All values are in
the station's own unit.
"""
from __future__ import annotations

import json
import statistics
from math import erf, exp, log, sqrt
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

CAL_PATH = Path(__file__).resolve().parents[1] / "calibration" / "station_calibration.json"

SHRINK_DAYS = 10          # pseudo-days pulling estimates toward the prior
DEFAULT_SIGMA_C = 1.6     # generic full-day sigma (°C) = probability.SIGMA_FLOOR_C + SIGMA_RANGE_C
MIN_SIGMA_C = 0.5


@dataclass
class StationCal:
    bias: dict[str, float] = field(default_factory=dict)
    sigma: float | None = None        # component sigma, full day ahead, station unit
    n_days: int = 0
    fitted_through: str | None = None
    weights: dict[str, float] = field(default_factory=dict)   # model weights (sum to 1)
    train_logloss: float | None = None                         # in-sample, for reference


@dataclass
class DayRecord:
    """One settled day at one station."""
    day: date
    model_max: dict[str, float]       # each model's forecast daily max (24 h lead), station unit
    truth: float | None               # settled high (bucket value / range midpoint); None if open-ended
    lo: int | None = None             # settled bucket bounds (whole degrees), when bounded
    hi: int | None = None


def _scale(unit: str) -> float:
    return 1.8 if unit == "F" else 1.0


def _phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _bounds(r: DayRecord) -> tuple[float, float]:
    if r.lo is not None and r.hi is not None:
        return r.lo - 0.5, r.hi + 0.5
    return r.truth - 0.5, r.truth + 0.5


def mixture_logloss(records: list[DayRecord], bias: dict[str, float], weights: dict[str, float],
                    s: float) -> float:
    """Mean −log P(settled bucket) under the weighted, bias-corrected mixture with sigma s."""
    total = 0.0
    for r in records:
        a, b = _bounds(r)
        ws = {m: weights.get(m, 0.0) for m in r.model_max}
        wsum = sum(ws.values()) or 1.0
        p = sum(w / wsum * (_phi((b - (r.model_max[m] + bias.get(m, 0.0))) / s)
                            - _phi((a - (r.model_max[m] + bias.get(m, 0.0))) / s))
                for m, w in ws.items())
        total += -log(max(p, 1e-4))
    return total / len(records)


def fit_station(records: list[DayRecord], unit: str) -> StationCal:
    usable = [r for r in records if r.truth is not None and r.model_max]
    n = len(usable)
    if n == 0:
        return StationCal()
    models = sorted({m for r in usable for m in r.model_max})

    # 1. Offsets: median error per model, shrunk toward 0 when few days.
    bias: dict[str, float] = {}
    for m in models:
        diffs = [r.truth - r.model_max[m] for r in usable if m in r.model_max]
        if diffs:
            k = len(diffs)
            bias[m] = round(statistics.median(diffs) * k / (k + SHRINK_DAYS), 2)

    # 2. Weights: inverse mean-squared error after bias correction (track record at this
    #    airport), blended toward equal weights when few days.
    floor_mse = (0.3 * _scale(unit)) ** 2
    inv = {}
    for m in models:
        errs = [(r.truth - (r.model_max[m] + bias[m])) ** 2 for r in usable if m in r.model_max]
        if errs:
            inv[m] = 1.0 / max(statistics.fmean(errs), floor_mse)
    tot = sum(inv.values())
    skill = {m: v / tot for m, v in inv.items()}
    lam = n / (n + SHRINK_DAYS)
    weights = {m: round(lam * skill[m] + (1 - lam) / len(skill), 4) for m in skill}

    # 3. Sigma: the value that would have scored best on these days (grid search), then a light
    #    pull toward the generic default in log-space when few days.
    sc = _scale(unit)
    grid = [round((0.25 + 0.05 * i) * sc, 3) for i in range(56)]      # 0.25 … 3.0 °C
    best_s = min(grid, key=lambda g: mixture_logloss(usable, bias, weights, g))
    prior = DEFAULT_SIGMA_C * sc
    k0 = SHRINK_DAYS / 2
    sigma = exp((n * log(best_s) + k0 * log(prior)) / (n + k0))
    return StationCal(
        bias=bias, sigma=round(sigma, 3), n_days=n,
        fitted_through=max(r.day for r in usable).isoformat(),
        weights=weights, train_logloss=round(mixture_logloss(usable, bias, weights, sigma), 3),
    )


def save(cals: dict[str, StationCal], path: Path | None = None, meta: dict | None = None) -> None:
    path = path or CAL_PATH          # resolved at call time (so tests can redirect it)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta or {}, "stations": {k: asdict(v) for k, v in sorted(cals.items())}}
    path.write_text(json.dumps(payload, indent=2))


def load(path: Path | None = None) -> dict[str, StationCal]:
    path = path or CAL_PATH
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out = {}
    for k, v in data.get("stations", {}).items():
        known = {f: v[f] for f in StationCal.__dataclass_fields__ if f in v}
        out[k] = StationCal(**known)
    return out
