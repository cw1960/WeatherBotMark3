"""End-to-end: one city through Daemon.evaluate_station with every external source faked."""
from datetime import datetime, timezone

import main as daemon_mod
from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.exchange.markets import Bucket, CityDay
from weatherbot.models import BotConfig, JevEvaluation, MetarObservation
from weatherbot.weather.forecast import ModelDay, StationForecast


class FakeDB:
    def __init__(self, trading_active=False):
        self.cfg = BotConfig(trading_active=trading_active, min_edge_pct=5)
        self.rows = []

    async def get_config(self):
        return self.cfg

    async def log_audit(self, row):
        self.rows.append(row)


class FakeJev:
    def __init__(self):
        self.seen = None

    async def evaluate(self, evidence):
        self.seen = evidence
        return JevEvaluation(direction="warmer", confidence=0.9, rationale="sunny", expected_high=27)


class FakeExecutor:
    def __init__(self):
        self.placed = []

    async def best_ask(self, token_id):
        return 0.30, 100.0

    async def place_fok(self, sig, *, live):
        from weatherbot.models import ExecutionResult
        self.placed.append((sig, live))
        return ExecutionResult(status="SIMULATED", raw={"order_type": "FOK"})


def _daemon(monkeypatch, st, today):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)   # skip __init__ (no real DB/http)
    d.db, d.jev, d.executor = FakeDB(), FakeJev(), FakeExecutor()
    d.tafs = {}
    d.afd = None
    labels = [("23°C or below", None, 23)] + [(f"{t}°C", t, t) for t in range(24, 33)] + [("33°C or higher", 33, None)]
    buckets = tuple(Bucket(st.city_slug, today, "e", "s", f"m{i}", f"0x{i}", lab, lo, hi, "C", f"yes{i}", f"no{i}",
                           True, True, False) for i, (lab, lo, hi) in enumerate(labels))
    d.markets = {(st.city_slug, today): CityDay(st.city_slug, today, "s", True, buckets, True)}
    now = datetime.now(timezone.utc)
    d.forecasts = {(st.icao_code, today): StationForecast(st.icao_code, "C", today, now, {
        "ecmwf_ifs025": ModelDay("ecmwf_ifs025", 24.0, 24.0, 6),
        "icon_seamless": ModelDay("icon_seamless", 24.0, 24.0, 6),
        "gfs_seamless": ModelDay("gfs_seamless", 25.0, 25.0, 6),
    })}
    return d


async def test_non_us_city_goes_through_jev_and_reaches_simulated_fok(monkeypatch):
    st = to_station(BY_SLUG["tokyo"])
    today = daemon_mod.local_today(st)
    d = _daemon(monkeypatch, st, today)
    # Observed 27°C already; models only expected ~24–25 → strong positive divergence
    metar = MetarObservation(icao_code="RJTT", observed_at=datetime.now(timezone.utc), temp=27,
                             high_so_far=27.0, unit="C", n_obs_today=10)
    await d.evaluate_station(st, metar, "cluster-1")

    assert d.jev.seen is not None and d.jev.seen["afd"] is None      # Jev ran without an AFD
    assert len(d.executor.placed) == 1
    sig, live = d.executor.placed[0]
    assert live is False and sig.outcome_label == "27°C"             # high can't go below 27
    row = d.db.rows[-1]
    assert row["decision"] == "simulated" and row["order_type"] == "FOK"
    assert row["model_high"] == 24.0 and row["obs_high_so_far"] == 27.0   # models expected 24
    assert row["divergence"] == 3.0
    assert row["jev_expected_high"] == 27 and row["evidence"]["station"]["icao"] == "RJTT"
    assert row["token_id"] == sig.token_id


async def test_missing_forecast_is_logged_as_skip(monkeypatch):
    st = to_station(BY_SLUG["london"])
    today = daemon_mod.local_today(st)
    d = _daemon(monkeypatch, st, today)
    d.forecasts = {}
    metar = MetarObservation(icao_code="EGLC", observed_at=datetime.now(timezone.utc), temp=15,
                             high_so_far=15.0, unit="C")
    await d.evaluate_station(st, metar, "c")
    assert d.db.rows[-1]["decision"] == "skip"
    assert "no model forecasts" in d.db.rows[-1]["skip_reason"]
    assert d.executor.placed == []
