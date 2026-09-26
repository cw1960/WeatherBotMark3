"""
Evidence bundle — everything we know about one city's daily high, packed for Jev
and stored verbatim in the audit log (trade_audit_logs.evidence) so every decision
can be reconstructed later.

Sources, per city:
  * METAR: observed high so far (local day), latest reading, number of reports
  * Model ensemble: every model's projected final high, the consensus, the spread
  * TAF: the airport's official forecast text (+ explicit max temp where given)
  * AFD: NWS forecaster discussion (US cities only)
  * Market: the buckets and which one each model's projection lands in
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..exchange.markets import CityDay
from ..models import MetarObservation, Station
from ..weather.forecast import StationForecast
from ..weather.taf import Taf


def build_evidence(
    *,
    station: Station,
    metar: MetarObservation | None,
    forecast: StationForecast | None,
    taf: Taf | None,
    afd: tuple[datetime, str] | None,
    city_day: CityDay | None,
    now: datetime,
) -> dict:
    obs_high = metar.high_so_far if metar else None
    local_now = now.astimezone(ZoneInfo(station.timezone))
    ev: dict = {
        "station": {
            "city": station.city_label, "icao": station.icao_code, "airport": station.airport_name,
            "unit": station.unit, "timezone": station.timezone,
            "local_time": local_now.strftime("%Y-%m-%d %H:%M"),
            "resolution_source": station.resolution_source,
        },
        "observations": None,
        "models": None,
        "taf": None,
        "afd": None,
        "market": None,
    }
    if metar:
        ev["observations"] = {
            "high_so_far": metar.high_so_far, "latest": metar.temp,
            "latest_at_utc": metar.observed_at.isoformat(), "reports_today": metar.n_obs_today,
            "latest_raw_metar": metar.raw,
        }
    if forecast:
        ev["models"] = forecast.summary(obs_high)
    if taf:
        tx = None
        if taf.max_temp_c is not None:
            tx_val = taf.max_temp_c * 9 / 5 + 32 if station.unit == "F" else taf.max_temp_c
            tx = {"max_temp": round(tx_val, 1), "day": taf.max_temp_day, "hour_utc": taf.max_temp_hour_utc}
        ev["taf"] = {"issued": taf.issued, "raw": taf.raw, "explicit_max": tx}
    if afd:
        issued, text = afd
        ev["afd"] = {"issued": issued.isoformat(), "text": text[:12000]}
    if city_day and city_day.buckets:
        by_model = {}
        if forecast:
            for name, val in forecast.projected_highs(obs_high).items():
                b = city_day.bucket_for(val)
                by_model[name] = b.label if b else None
        ev["market"] = {
            "buckets": [b.label for b in city_day.buckets],
            "bucket_per_model": by_model,
            "distinct_buckets_across_models": len({v for v in by_model.values() if v}),
        }
    return ev
