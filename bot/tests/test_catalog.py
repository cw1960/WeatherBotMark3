"""Station catalog integrity — the mapping every trade depends on."""
from zoneinfo import ZoneInfo

from weatherbot.catalog import BY_SLUG, CATALOG


def test_slugs_and_icaos_unique():
    assert len({c.city_slug for c in CATALOG}) == len(CATALOG)
    assert len({c.icao for c in CATALOG}) == len(CATALOG)


def test_icao_format_and_timezones():
    for c in CATALOG:
        assert len(c.icao) == 4 and c.icao.isalnum() and c.icao.isupper(), c
        ZoneInfo(c.timezone)  # raises if invalid


def test_units_fahrenheit_only_in_us():
    for c in CATALOG:
        assert (c.unit == "F") == (c.country == "US"), c.city_slug


def test_every_active_city_has_a_rules_phrase():
    for c in CATALOG:
        if c.active:
            assert c.rules_phrase, c.city_slug


def test_known_traps():
    assert BY_SLUG["nyc"].icao == "KLGA"        # LaGuardia, NOT Central Park (KNYC)
    assert BY_SLUG["dallas"].icao == "KDAL"     # Love Field, not DFW
    assert BY_SLUG["houston"].icao == "KHOU"    # Hobby, not Bush Intercontinental
    assert BY_SLUG["london"].icao == "EGLC"     # London City, not Heathrow
    assert BY_SLUG["denver"].icao == "KBKF"     # Buckley SFB, not DEN
    assert not BY_SLUG["hong-kong"].active      # resolves on HK Observatory, not a METAR
