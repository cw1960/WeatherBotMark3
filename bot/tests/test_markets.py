"""Market discovery: slugs, bucket parsing, rules check, bucket selection."""
import json
from datetime import date

import httpx
import pytest

from weatherbot.exchange.markets import (
    GammaClient, event_slug, normalise, parse_bucket, resolution_round, rules_mention,
)


def test_event_slug_matches_polymarket_format():
    assert event_slug("nyc", date(2026, 9, 26)) == "highest-temperature-in-nyc-on-september-26-2026"
    assert event_slug("mexico-city", date(2026, 10, 3)) == "highest-temperature-in-mexico-city-on-october-3-2026"


@pytest.mark.parametrize("label,expected", [
    ("57°F or below", (None, 57, "F")),
    ("58-59°F", (58, 59, "F")),
    ("76°F or higher", (76, None, "F")),
    ("28°C", (28, 28, "C")),
    ("37°C or higher", (37, None, "C")),
    ("10°C or below", (None, 10, "C")),
    ("-3°C", (-3, -3, "C")),
    ("-5--4°F", (-5, -4, "F")),
])
def test_parse_bucket(label, expected):
    assert parse_bucket(label) == expected


def test_parse_bucket_rejects_garbage():
    with pytest.raises(ValueError):
        parse_bucket("Other")


def test_normalise_handles_accents_and_apostrophes():
    assert normalise("Esenboğa Intl") == "esenbogaintl"
    assert normalise("Chicago O’Hare") == normalise("Chicago O'Hare") == "chicagoohare"
    assert rules_mention("recorded by NOAA at the Benito Juárez International Airport Station", "Benito Juarez")
    assert not rules_mention("recorded by NOAA at Central Park", "LaGuardia")
    assert not rules_mention("anything", None)


def test_resolution_rounding_is_half_up():
    assert resolution_round(72.5) == 73
    assert resolution_round(72.49) == 72
    assert resolution_round(-0.5) == 0


# A trimmed copy of the real NYC event (Gamma API, 2026-09-26).
NYC_RULES = ("This market will resolve to the temperature bracket containing the highest temperature "
             "recorded by NOAA at LaGuardia Airport Station in degrees Fahrenheit on September 26, 2026.")


def _nyc_event(rules=NYC_RULES):
    labels = ["57°F or below"] + [f"{a}-{a+1}°F" for a in range(58, 76, 2)] + ["76°F or higher"]
    return [{
        "id": "1073164", "slug": "highest-temperature-in-nyc-on-september-26-2026", "negRisk": True,
        "markets": [{
            "id": str(4903536 + i), "conditionId": f"0x{i:064x}", "groupItemTitle": lab,
            "outcomes": json.dumps(["Yes", "No"]),
            "clobTokenIds": json.dumps([str(1000 + i), str(2000 + i)]),
            "description": rules, "closed": False,
        } for i, lab in enumerate(labels)],
    }]


def _client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["slug"] == "highest-temperature-in-nyc-on-september-26-2026"
        return httpx.Response(200, json=payload)
    return GammaClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_fetch_city_day_parses_real_shape():
    cd = await _client(_nyc_event()).fetch_city_day(
        city_slug="nyc", target_date=date(2026, 9, 26), unit="F", rules_phrase="LaGuardia")
    assert cd.found and cd.rules_ok and cd.problem is None
    assert len(cd.buckets) == 11
    assert cd.buckets[0].label == "57°F or below" and cd.buckets[-1].label == "76°F or higher"
    b = cd.bucket_for(72.4)
    assert b.label == "72-73°F" and b.token_yes == "1008" and b.token_no == "2008"
    assert cd.bucket_for(50).label == "57°F or below"
    assert cd.bucket_for(90).label == "76°F or higher"


async def test_station_change_in_rules_blocks_trading():
    moved = NYC_RULES.replace("LaGuardia Airport", "Central Park")
    cd = await _client(_nyc_event(moved)).fetch_city_day(
        city_slug="nyc", target_date=date(2026, 9, 26), unit="F", rules_phrase="LaGuardia")
    assert cd.found and not cd.rules_ok
    assert "NOT TRADEABLE" in cd.problem


async def test_wrong_unit_blocks_trading():
    cd = await _client(_nyc_event()).fetch_city_day(
        city_slug="nyc", target_date=date(2026, 9, 26), unit="C", rules_phrase="LaGuardia")
    assert not cd.rules_ok


async def test_missing_event():
    c = GammaClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))))
    cd = await c.fetch_city_day(city_slug="nyc", target_date=date(2026, 9, 26), unit="F", rules_phrase="LaGuardia")
    assert not cd.found and cd.problem == "no event listed"
