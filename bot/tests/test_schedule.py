"""Decision windows: ~60 min before each city's typical peak, in local time."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from weatherbot import schedule


def test_window_is_an_hour_before_the_typical_peak():
    tz = ZoneInfo("America/New_York")
    a, b = schedule.decision_window(15 * 60 + 30, date(2026, 9, 27), tz)
    assert (a.hour, a.minute, b.hour, b.minute) == (14, 15, 14, 45)


def test_is_due_uses_each_city_local_clock(tmp_path):
    p = tmp_path / "peaks.json"
    p.write_text(json.dumps({"stations": {"KLGA": {"median_peak_min": 15 * 60}, "EGLC": {"median_peak_min": 14 * 60}}}))
    peaks = schedule.load_peaks(p)
    assert peaks == {"KLGA": 900, "EGLC": 840}
    t = datetime(2026, 9, 27, 13, 0, tzinfo=ZoneInfo("Europe/London"))        # = 08:00 New York
    assert schedule.is_due("EGLC", "Europe/London", peaks, t)
    assert not schedule.is_due("KLGA", "America/New_York", peaks, t)
    assert not schedule.is_due("RJTT", "Asia/Tokyo", peaks, t)                 # unknown → never
    assert schedule.load_peaks(tmp_path / "nope.json") == {}
