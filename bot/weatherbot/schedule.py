"""
When does the bot decide for each city?  ~60 minutes before that city's typical daily peak.

Christopher's idea, confirmed by the backtests: the high comes in a narrow afternoon window that
differs by city (Shanghai ~11:00, Madrid ~17:00), and a trade has to be placed BEFORE it. At
local midnight the market already knows everything the bot knows; ~1 h before the usual peak,
the bot's view blended with the market's beat the market alone (intraday_backtest.py).

peak_times.py writes bot/calibration/peak_times.json (typical peak per airport). Each city is
evaluated only inside its decision window:

    typical peak − DECISION_OFFSET_MIN − WINDOW_EARLY_MIN   …   typical peak − DECISION_OFFSET_MIN + WINDOW_LATE_MIN

(e.g. typical peak 15:00 → window 13:45–14:15 local). Cities with no typical peak on file are
never evaluated — the bot won't guess a decision time.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PEAKS_PATH = Path(__file__).resolve().parents[1] / "calibration" / "peak_times.json"
DECISION_OFFSET_MIN = 60
WINDOW_EARLY_MIN = 15
WINDOW_LATE_MIN = 15


def load_peaks(path: Path | None = None) -> dict[str, int]:
    """ICAO → typical peak time in minutes after local midnight ({} if the file is missing)."""
    path = path or PEAKS_PATH
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {icao: int(v["median_peak_min"]) for icao, v in data.get("stations", {}).items()
            if v.get("median_peak_min") is not None}


def decision_window(peak_min: int, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    centre = datetime.combine(day, time.min, tzinfo=tz) + timedelta(minutes=peak_min - DECISION_OFFSET_MIN)
    return centre - timedelta(minutes=WINDOW_EARLY_MIN), centre + timedelta(minutes=WINDOW_LATE_MIN)


def window_for(icao: str, timezone: str, peaks: dict[str, int], now: datetime) -> tuple[datetime, datetime] | None:
    """Today's (local) decision window for this airport, or None if no typical peak is known."""
    if icao not in peaks:
        return None
    tz = ZoneInfo(timezone)
    return decision_window(peaks[icao], now.astimezone(tz).date(), tz)


def is_due(icao: str, timezone: str, peaks: dict[str, int], now: datetime) -> bool:
    w = window_for(icao, timezone, peaks, now)
    return w is not None and w[0] <= now <= w[1]
