"""TAF parsing and the evidence bundle Jev receives."""
from datetime import date, datetime, timezone

from weatherbot.ai.evidence import build_evidence
from weatherbot.catalog import BY_SLUG, to_station
from weatherbot.exchange.markets import Bucket, CityDay
from weatherbot.models import MetarObservation
from weatherbot.weather.forecast import ModelDay, StationForecast
from weatherbot.weather.taf import Taf, parse_tx


def test_parse_tx_groups():
    assert parse_tx("TAF RJTT 252305Z 2600/2706 36016KT 9999 TX27/2605Z TN19/2620Z") == (27.0, 26, 5)
    assert parse_tx("TAF UUWW 260500Z 2606/2706 TXM02/2612Z TNM08/2703Z") == (-2.0, 26, 12)
    assert parse_tx("TAF KLGA 260239Z 2603/2706 03024G38KT P6SM -RA") == (None, None, None)


def _bucket(label, lo, hi):
    return Bucket("tokyo", date(2026, 9, 26), "e", "s", label, "0x" + label, label, lo, hi, "C",
                  "y", "n", True, True, False)


def test_evidence_bundle_for_non_us_city():
    st = to_station(BY_SLUG["tokyo"])
    now = datetime(2026, 9, 26, 3, tzinfo=timezone.utc)
    metar = MetarObservation(icao_code="RJTT", observed_at=now, temp=23, high_so_far=23.4, unit="C", n_obs_today=8)
    fc = StationForecast("RJTT", "C", date(2026, 9, 26), now, {
        "ecmwf_ifs025": ModelDay("ecmwf_ifs025", 25.0, 25.0, 12),
        "icon_seamless": ModelDay("icon_seamless", 26.0, 26.0, 12),
        "jma_seamless": ModelDay("jma_seamless", 24.2, 24.2, 12),
    })
    cd = CityDay("tokyo", date(2026, 9, 26), "s", True, (
        _bucket("23°C or below", None, 23), _bucket("24°C", 24, 24), _bucket("25°C", 25, 25),
        _bucket("26°C or higher", 26, None)), True)
    taf = Taf("RJTT", "2026-09-25T23:05:00Z", "TAF RJTT ... TX27/2605Z", 27.0, 26, 5)
    ev = build_evidence(station=st, metar=metar, forecast=fc, taf=taf, afd=None, city_day=cd, now=now)

    assert ev["station"]["icao"] == "RJTT" and ev["station"]["local_time"] == "2026-09-26 12:00"
    assert ev["observations"]["high_so_far"] == 23.4
    assert ev["models"]["consensus_projected_high"] == 25.0
    assert ev["models"]["spread"] == 1.8
    assert ev["taf"]["explicit_max"] == {"max_temp": 27.0, "day": 26, "hour_utc": 5}
    assert ev["afd"] is None                          # no NWS discussion abroad — Jev still runs
    assert ev["market"]["bucket_per_model"] == {"ecmwf_ifs025": "25°C", "icon_seamless": "26°C or higher", "jma_seamless": "24°C"}
    assert ev["market"]["distinct_buckets_across_models"] == 3


def test_taf_max_converted_for_fahrenheit_city():
    st = to_station(BY_SLUG["miami"])
    now = datetime(2026, 9, 26, 15, tzinfo=timezone.utc)
    taf = Taf("KMIA", "x", "TAF KMIA ... TX32/2619Z", 32.0, 26, 19)
    ev = build_evidence(station=st, metar=None, forecast=None, taf=taf, afd=None, city_day=None, now=now)
    assert ev["taf"]["explicit_max"]["max_temp"] == 89.6
