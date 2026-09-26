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
        self.calls = 0

    async def evaluate(self, state):
        self.seen, self.calls = state, self.calls + 1
        return JevEvaluation(direction="neutral", confidence=0.8, rationale="as forecast",
                             shift_probs={"as_forecast": 1.0}, p_peak_passed=0.1)


class FakeExecutor:
    def __init__(self, ask_27=0.75):
        self.placed, self.ask_27 = [], ask_27

    async def book_quotes(self, token_ids):
        # 27°C: ask ask_27, bid 5¢ below; everything else ask 0.05 / bid 0.01
        from weatherbot.exchange.polymarket import Quote
        return {t: (Quote(self.ask_27, 100.0, self.ask_27 - 0.05) if t == "yes4" else Quote(0.05, 100.0, 0.01))
                for t in token_ids}

    async def place_fok(self, sig, *, live):
        from weatherbot.models import ExecutionResult
        self.placed.append((sig, live))
        return ExecutionResult(status="SIMULATED", raw={"order_type": "FOK"})


def _daemon(monkeypatch, st, today):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)   # skip __init__ (no real DB/http)
    d.db, d.jev, d.executor = FakeDB(), FakeJev(), FakeExecutor()
    d.tafs, d.afds, d._jev_cache, d.calibration = {}, {}, {}, {}
    d.intraday, d.intraday_default, d._done, d._announced = {}, None, set(), set()
    labels = [("23°C or below", None, 23)] + [(f"{t}°C", t, t) for t in range(24, 33)] + [("33°C or higher", 33, None)]
    buckets = tuple(Bucket(st.city_slug, today, "e", "s", f"m{i}", f"0x{i}", lab, lo, hi, "C", f"yes{i}", f"no{i}",
                           True, True, False) for i, (lab, lo, hi) in enumerate(labels))
    d.markets = {(st.city_slug, today): CityDay(st.city_slug, today, "s", True, buckets, True)}
    now = datetime.now(timezone.utc)
    d.forecasts = {(st.icao_code, today): StationForecast(st.icao_code, "C", today, now, {
        # models expected ~24–25 all day; little heating left
        "ecmwf_ifs025": ModelDay("ecmwf_ifs025", 24.0, 23.0, 2, 24.0, 12),
        "icon_seamless": ModelDay("icon_seamless", 24.0, 23.0, 2, 24.0, 12),
        "gfs_seamless": ModelDay("gfs_seamless", 25.0, 24.0, 2, 25.0, 12),
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

    assert d.jev.calls == 1 and "forecaster_discussion" not in d.jev.seen   # Jev ran without an AFD
    assert len(d.executor.placed) == 1
    sig, live = d.executor.placed[0]
    # Observed 27°C late in the day: the high can't fall, so 27°C is very likely — at 0.75
    # the market is underpricing it.
    assert live is False and sig.outcome_label == "27°C" and sig.market_price == 0.75
    assert sig.model_probability > 0.8 and sig.price >= 0.75       # blended with the market's ~71%
    assert "blend" in d.db.rows[-1]["evidence"]["rule"]
    assert (st.city_slug, today) in d._done                          # one order per city per day
    row = d.db.rows[-1]
    assert row["decision"] == "simulated" and row["order_type"] == "FOK"
    assert row["model_high"] == 24.0 and row["obs_high_so_far"] == 27.0 and row["divergence"] == 3.0
    assert row["evidence"]["station"]["icao"] == "RJTT" and len(row["evidence"]["buckets"]) == 11
    assert row["token_id"] == sig.token_id == "yes4"

    # Same inputs next cycle → Jev is NOT called again (cached by fingerprint)
    await d.evaluate_station(st, metar, "cluster-2")
    assert d.jev.calls == 1


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


async def test_huge_gap_with_market_is_refused(monkeypatch):
    st = to_station(BY_SLUG["tokyo"])
    today = daemon_mod.local_today(st)
    d = _daemon(monkeypatch, st, today)
    d.executor = FakeExecutor(ask_27=0.20)   # market says 20%, we say ~85% → we're probably wrong
    metar = MetarObservation(icao_code="RJTT", observed_at=datetime.now(timezone.utc), temp=27,
                             high_so_far=27.0, unit="C", n_obs_today=10)
    await d.evaluate_station(st, metar, "c")
    assert d.executor.placed == []
    assert "implausibly large" in d.db.rows[-1]["skip_reason"]


async def test_runs_without_jev_key(monkeypatch):
    st = to_station(BY_SLUG["tokyo"])
    today = daemon_mod.local_today(st)
    d = _daemon(monkeypatch, st, today)
    d.jev = None                                      # no TYPESAFE_API_KEY
    metar = MetarObservation(icao_code="RJTT", observed_at=datetime.now(timezone.utc), temp=27,
                             high_so_far=27.0, unit="C", n_obs_today=10)
    await d.evaluate_station(st, metar, "c")
    assert len(d.executor.placed) == 1 and d.executor.placed[0][1] is False
    assert "off" in d.db.rows[-1]["evidence"]["jev"]


async def test_incomplete_market_prices_skip(monkeypatch):
    st = to_station(BY_SLUG["tokyo"])
    today = daemon_mod.local_today(st)
    d = _daemon(monkeypatch, st, today)

    class Sparse(FakeExecutor):
        async def book_quotes(self, token_ids):
            q = await super().book_quotes(token_ids)
            return {t: v for t, v in q.items() if t in ("yes4", "yes5")}
    d.executor = Sparse()
    metar = MetarObservation(icao_code="RJTT", observed_at=datetime.now(timezone.utc), temp=27,
                             high_so_far=27.0, unit="C", n_obs_today=10)
    await d.evaluate_station(st, metar, "c")
    assert d.executor.placed == [] and "incomplete" in d.db.rows[-1]["skip_reason"]


def test_only_cities_in_their_window_are_due(monkeypatch):
    from zoneinfo import ZoneInfo
    tok, lon = to_station(BY_SLUG["tokyo"]), to_station(BY_SLUG["london"])
    d = _daemon(monkeypatch, tok, daemon_mod.local_today(tok))
    d.peaks = {"RJTT": 14 * 60, "EGLC": 15 * 60}          # typical peaks 14:00 / 15:00 local
    now = datetime(2026, 9, 27, 12, 55, tzinfo=ZoneInfo("Asia/Tokyo"))   # Tokyo window 12:45–13:15
    assert [s.city_slug for s in d.due_stations([tok, lon], now)] == ["tokyo"]
    d._done.add(("tokyo", now.date()))                    # already traded today → not due again
    assert d.due_stations([tok, lon], now) == []
    late = datetime(2026, 9, 27, 13, 20, tzinfo=ZoneInfo("Asia/Tokyo"))
    d._done.clear()
    assert d.due_stations([tok, lon], late) == []         # window over
    d.peaks = {}
    assert d.due_stations([tok, lon], now) == []          # no peak time known → never traded
