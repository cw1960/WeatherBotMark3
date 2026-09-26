"""Probability engine: sums to 1, respects the observed floor, shifts, peak-passed, buckets."""
from datetime import date, datetime, timezone

import pytest

from weatherbot.exchange.markets import Bucket
from weatherbot.probability import (
    bucket_probabilities, distribution, forecast_distribution, model_mean, sigma,
)
from weatherbot.weather.forecast import ModelDay, StationForecast


def _b(label, lo, hi, unit="C"):
    return Bucket("x", date(2026, 9, 26), "e", "s", label, label, label, lo, hi, unit, "y", "n", True, True, False)


C_BUCKETS = [_b("23°C or below", None, 23)] + [_b(f"{t}°C", t, t) for t in range(24, 31)] + [_b("31°C or higher", 31, None)]


def test_distribution_sums_to_one_and_never_below_observed():
    d = distribution(observed_high=25.3, means=[24.0, 26.0, 27.5], hours_remaining=6, unit="C")
    assert sum(d.values()) == pytest.approx(1.0)
    assert min(d) == 25                     # 25.3 rounds to 25: nothing below it is possible


def test_peak_passed_puts_mass_on_observed():
    d = distribution(observed_high=25.0, means=[28.0], hours_remaining=6, unit="C", p_peak_passed=0.9)
    assert d[25] >= 0.88     # ~0.9 minus the small never-certain allowance


def test_no_hours_left_is_likely_but_never_certain():
    d = distribution(observed_high=25.6, means=[29.0], hours_remaining=0, unit="C")
    assert min(d) == 26 and 0.9 < d[26] < 1.0           # observed bucket near-certain …
    assert d.get(27, 0) > 0.0                           # … but never exactly certain
    assert sum(d.values()) == pytest.approx(1.0)


def test_sigma_scales_with_time_and_unit():
    assert sigma(0, "C") < sigma(6, "C") < sigma(12, "C") == sigma(20, "C")
    assert sigma(6, "F") == pytest.approx(sigma(6, "C") * 1.8)


def test_bias_persistence_needs_enough_elapsed_hours():
    # station has run 2° warmer than this model so far
    assert model_mean(remaining_high=25, elapsed_high=23, observed_high=25, hours_elapsed=6, unit="C") == 26.0
    assert model_mean(remaining_high=25, elapsed_high=23, observed_high=25, hours_elapsed=1, unit="C") == 25.0


def _fc(remaining, hours_remaining=6, elapsed_high=None, hours_elapsed=8):
    now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    return StationForecast("X", "C", date(2026, 9, 26), now, {
        f"m{i}": ModelDay(f"m{i}", r, r, hours_remaining, elapsed_high, hours_elapsed) for i, r in enumerate(remaining)})


def test_jev_warmer_shifts_mass_up():
    fc = _fc([26.0, 26.0, 26.0])
    base = bucket_probabilities(forecast_distribution(fc, 22.0, "C"), C_BUCKETS)
    warm = bucket_probabilities(forecast_distribution(fc, 22.0, "C", jev_shift_probs={"much_warmer": 1.0}), C_BUCKETS)
    assert sum(base.values()) == pytest.approx(1.0) and sum(warm.values()) == pytest.approx(1.0)
    assert max(base, key=base.get) == "26°C"
    assert max(warm, key=warm.get) == "28°C"


def test_open_ended_buckets_capture_tails():
    fc = _fc([35.0])
    probs = bucket_probabilities(forecast_distribution(fc, 20.0, "C"), C_BUCKETS)
    assert probs["31°C or higher"] > 0.99
