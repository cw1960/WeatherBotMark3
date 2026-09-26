"""
Multi-model forecast for every station, from Open-Meteo (one API, many national models).

For each station we fetch HOURLY 2 m temperature from every model below, then compute:
  * full_day_high  — the model's max over the whole local day
  * remaining_high — the model's max over the hours still ahead today (local)
  * projected_high — max(observed high so far, remaining_high): what the day's high will
                     end up as IF that model is right about the rest of the day
Across models we report the consensus (median projected high) and the spread
(max − min), which is itself a signal: when models disagree, markets misprice more.

Models:
  * Global, for every city: ECMWF IFS 0.25°, ECMWF AIFS (AI model), DWD ICON (seamless:
    uses ICON-D2/EU at high resolution where available), NOAA GFS, Canada GEM,
    Japan JMA, UK Met Office, Korea KMA, China CMA.
  * US only: NOAA HRRR (3 km, hourly updates) — the sharpest short-range US model.
Any model name Open-Meteo rejects is dropped automatically (logged once), so a renamed
or retired model can't take the whole feed down.

Always queried by the station's catalogued lat/lon — never by city name.

LICENSING: the free api.open-meteo.com endpoint is for non-commercial use. For live
trading set OPEN_METEO_API_KEY in bot/.env; requests then go to customer-api.open-meteo.com.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog

from ..models import Station
from ..settings import settings

log = structlog.get_logger(__name__)

GLOBAL_MODELS: tuple[str, ...] = (
    "ecmwf_ifs025", "ecmwf_aifs025", "icon_seamless", "gfs_seamless",
    "gem_seamless", "jma_seamless", "ukmo_seamless", "kma_seamless", "cma_grapes",
)
US_ONLY_MODELS: tuple[str, ...] = ("gfs_hrrr",)
PRIMARY_US, PRIMARY_GLOBAL = "gfs_hrrr", "ecmwf_ifs025"
BATCH = 10  # locations per request


def model_for(station: Station) -> str:
    """The single 'headline' model reported alongside the consensus."""
    return PRIMARY_US if station.country == "US" else PRIMARY_GLOBAL


def models_for(station: Station) -> tuple[str, ...]:
    return (US_ONLY_MODELS + GLOBAL_MODELS) if station.country == "US" else GLOBAL_MODELS


def _to_unit(c: float, unit: str) -> float:
    return round(c * 9 / 5 + 32, 1) if unit == "F" else round(c, 1)


@dataclass
class ModelDay:
    model: str
    full_day_high: float           # station unit
    remaining_high: float | None   # None when no hours remain today
    hours_remaining: int
    elapsed_high: float | None = None  # model's max over hours already past today (for bias vs obs)
    hours_elapsed: int = 0


@dataclass
class StationForecast:
    icao_code: str
    unit: str
    target_date: date
    fetched_at: datetime
    models: dict[str, ModelDay] = field(default_factory=dict)

    def projected_highs(self, observed_high: float | None) -> dict[str, float]:
        out = {}
        for name, m in self.models.items():
            base = m.remaining_high if m.remaining_high is not None else m.full_day_high
            out[name] = max(base, observed_high) if observed_high is not None else base
        return out

    def consensus(self, observed_high: float | None) -> float | None:
        vals = list(self.projected_highs(observed_high).values())
        return round(statistics.median(vals), 1) if vals else None

    def raw_consensus(self) -> float | None:
        """Median of the models' own full-day highs — what they expect WITHOUT knowing the
        observations. Observed high minus this is the divergence the strategy trades on."""
        vals = [m.full_day_high for m in self.models.values()]
        return round(statistics.median(vals), 1) if vals else None

    def bias_so_far(self, observed_high: float | None, min_hours: int = 3) -> float | None:
        """Median of (observed high so far − model's max over hours already past today).
        Positive = station running warmer than the models. None early in the day."""
        if observed_high is None:
            return None
        vals = [observed_high - m.elapsed_high for m in self.models.values()
                if m.elapsed_high is not None and m.hours_elapsed >= min_hours]
        return round(statistics.median(vals), 1) if vals else None

    def spread(self, observed_high: float | None) -> float | None:
        vals = list(self.projected_highs(observed_high).values())
        return round(max(vals) - min(vals), 1) if len(vals) >= 2 else None

    def summary(self, observed_high: float | None) -> dict:
        """JSON-able snapshot stored in the audit log and shown to Jev."""
        return {
            "unit": self.unit,
            "target_date": self.target_date.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "observed_high_so_far": observed_high,
            "consensus_full_day_high": self.raw_consensus(),
            "running_vs_models_so_far": self.bias_so_far(observed_high),
            "consensus_projected_high": self.consensus(observed_high),
            "spread": self.spread(observed_high),
            "models": {
                name: {
                    "full_day_high": m.full_day_high,
                    "remaining_high": m.remaining_high,
                    "hours_remaining": m.hours_remaining,
                    "projected_high": self.projected_highs(observed_high)[name],
                }
                for name, m in sorted(self.models.items())
            },
        }


def summarise_hourly(
    station: Station, times_utc: list[int], temps_by_model: dict[str, list[float | None]],
    target_date: date, now: datetime,
) -> StationForecast:
    """Pure function: hourly series (°C, unix UTC) → per-model day stats in the station unit."""
    tz = ZoneInfo(station.timezone)
    day_start = datetime.combine(target_date, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    fc = StationForecast(station.icao_code, station.unit, target_date, now)
    stamps = [datetime.fromtimestamp(t, tz=timezone.utc) for t in times_utc]
    for model, temps in temps_by_model.items():
        day, rest, past = [], [], []
        for ts, t in zip(stamps, temps):
            if t is None or not (day_start <= ts < day_end):
                continue
            day.append(t)
            (rest if ts >= now else past).append(t)
        if len(day) < 12:  # model doesn't cover this day / location (e.g. HRRR abroad)
            continue
        fc.models[model] = ModelDay(
            model=model,
            full_day_high=_to_unit(max(day), station.unit),
            remaining_high=_to_unit(max(rest), station.unit) if rest else None,
            hours_remaining=len(rest),
            elapsed_high=_to_unit(max(past), station.unit) if past else None,
            hours_elapsed=len(past),
        )
    return fc


def parse_location(payload: dict, models: tuple[str, ...]) -> tuple[list[int], dict[str, list]]:
    """Accepts either key style Open-Meteo may use: 'temperature_2m_<model>' (several models)
    or plain 'temperature_2m' (a single model)."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    series: dict[str, list] = {}
    for m in models:
        if (vals := hourly.get(f"temperature_2m_{m}")) is not None:
            series[m] = vals
    if not series and len(models) == 1 and "temperature_2m" in hourly:
        series[models[0]] = hourly["temperature_2m"]
    return times, series


class ForecastClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._rejected: set[str] = set()

    @property
    def _base(self) -> str:
        key = settings.open_meteo_api_key
        return "https://customer-api.open-meteo.com/v1/forecast" if key else "https://api.open-meteo.com/v1/forecast"

    async def _request(self, stations: list[Station], models: tuple[str, ...]) -> list[dict]:
        params = {
            "latitude": ",".join(str(s.latitude) for s in stations),
            "longitude": ",".join(str(s.longitude) for s in stations),
            "hourly": "temperature_2m",
            "models": ",".join(models),
            "timeformat": "unixtime",
            "past_days": 1,
            "forecast_days": 3,
            "temperature_unit": "celsius",
        }
        if key := settings.open_meteo_api_key:
            params["apikey"] = key
        r = await self._http.get(self._base, params=params, timeout=45)
        if r.status_code == 400:
            raise ValueError((r.json() or {}).get("reason", "bad request"))
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else [data]

    async def _fetch_group(self, stations: list[Station], models: tuple[str, ...]) -> list[dict]:
        models = tuple(m for m in models if m not in self._rejected)
        try:
            return await self._request(stations, models)
        except ValueError as exc:
            # Find which model name(s) are invalid by probing individually, then retry once.
            log.warning("open_meteo_rejected_request", reason=str(exc))
            for m in models:
                try:
                    await self._request(stations[:1], (m,))
                except ValueError:
                    self._rejected.add(m)
                    log.warning("open_meteo_model_dropped", model=m)
            good = tuple(m for m in models if m not in self._rejected)
            return await self._request(stations, good) if good else []

    async def fetch_all(
        self, stations: list[Station], now: datetime | None = None
    ) -> dict[tuple[str, date], StationForecast]:
        """Forecasts for local today AND tomorrow at every station, keyed (icao, date)."""
        now = now or datetime.now(timezone.utc)
        out: dict[tuple[str, date], StationForecast] = {}
        usable = [s for s in stations if s.latitude is not None and s.longitude is not None]
        groups = [
            ([s for s in usable if s.country == "US"], US_ONLY_MODELS + GLOBAL_MODELS),
            ([s for s in usable if s.country != "US"], GLOBAL_MODELS),
        ]
        for group, models in groups:
            for i in range(0, len(group), BATCH):
                chunk = group[i:i + BATCH]
                try:
                    payloads = await self._fetch_group(chunk, models)
                except httpx.HTTPError as exc:
                    log.error("open_meteo_failed", error=str(exc), stations=[s.icao_code for s in chunk])
                    continue
                active = tuple(m for m in models if m not in self._rejected)
                for st, payload in zip(chunk, payloads):
                    times, series = parse_location(payload, active)
                    today = now.astimezone(ZoneInfo(st.timezone)).date()
                    for day in (today, today + timedelta(days=1)):
                        fc = summarise_hourly(st, times, series, day, now)
                        if fc.models:
                            out[(st.icao_code, day)] = fc
        return out
