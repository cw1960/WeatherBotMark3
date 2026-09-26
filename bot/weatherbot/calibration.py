"""
Per-station calibration learned from history (see backtest.py), used by probability.py.

Forecast models predict the average over a grid box several km wide, not one airport
thermometer, so each station runs systematically warmer or cooler than each model. For every
station we learn:
  * bias[model]  — median of (settled high − model's forecast high), shrunk toward 0 when few
                   days are available
  * sigma        — spread of the settled high around the bias-corrected ensemble, with a full
                   day of heating ahead (the "how uncertain is a day-ahead forecast here" number),
                   shrunk toward the generic default when few days are available

Stored in bot/calibration/station_calibration.json (committed; not secret). All values are in
the station's own unit.
"""
from __future__ import annotations

import json
import statistics
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
    sigma: float | None = None        # per-model component sigma, full day ahead, station unit
    n_days: int = 0
    fitted_through: str | None = None


@dataclass
class DayRecord:
    """One settled day at one station."""
    day: date
    model_max: dict[str, float]       # each model's forecast daily max (24 h lead), station unit
    truth: float | None               # settled high (bucket value / range midpoint); None if open-ended


def _scale(unit: str) -> float:
    return 1.8 if unit == "F" else 1.0


def fit_station(records: list[DayRecord], unit: str) -> StationCal:
    usable = [r for r in records if r.truth is not None and r.model_max]
    n = len(usable)
    if n == 0:
        return StationCal()
    models = sorted({m for r in usable for m in r.model_max})
    bias: dict[str, float] = {}
    for m in models:
        diffs = [r.truth - r.model_max[m] for r in usable if m in r.model_max]
        if diffs:
            k = len(diffs)
            bias[m] = round(statistics.median(diffs) * k / (k + SHRINK_DAYS), 2)

    # Residuals of the bias-corrected ensemble mean, and the between-model variance.
    resid, between = [], []
    for r in usable:
        corrected = [r.model_max[m] + bias.get(m, 0.0) for m in r.model_max]
        mean = statistics.fmean(corrected)
        resid.append(r.truth - mean)
        if len(corrected) >= 2:
            between.append(statistics.pvariance(corrected))
    s2 = statistics.fmean([e * e for e in resid])
    # The mixture already carries the between-model spread; the component sigma is what's left.
    comp2 = max(s2 - (statistics.fmean(between) if between else 0.0), (MIN_SIGMA_C * _scale(unit)) ** 2)
    prior2 = (DEFAULT_SIGMA_C * _scale(unit)) ** 2
    sigma = ((n * comp2 + SHRINK_DAYS * prior2) / (n + SHRINK_DAYS)) ** 0.5
    return StationCal(bias=bias, sigma=round(sigma, 3), n_days=n,
                      fitted_through=max(r.day for r in usable).isoformat())


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
    return {k: StationCal(**v) for k, v in data.get("stations", {}).items()}
