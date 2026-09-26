"""
Market discovery for every Polymarket "Highest temperature in <city> on <date>?" event.

Polymarket lists a fresh event per city per local day, slugged:
    highest-temperature-in-{city_slug}-on-{month}-{day}-{year}
Each event holds ~11 Yes/No markets, one per temperature bucket, e.g.
    '57°F or below', '58-59°F', ..., '76°F or higher'   (US, 2-degree buckets)
    '27°C or below', '28°C', ..., '37°C or higher'       (elsewhere, 1-degree buckets)

This module:
  * builds the slug for a (city, date),
  * fetches the event from the public Gamma API (no auth),
  * parses every bucket into integer bounds + Yes/No CLOB token ids,
  * verifies the rules text still names the station in our catalog (rules_ok),
  * maps an observed/forecast temperature to the bucket that would win.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from math import floor
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


# --------------------------------------------------------------------------- helpers
def event_slug(city_slug: str, day: date) -> str:
    return f"highest-temperature-in-{city_slug}-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"


def normalise(text: str) -> str:
    """Lower-case, strip accents and every non-alphanumeric char.
    'Esenboğa' → 'esenboga', "O’Hare" → 'ohare', 'Bao'an' → 'baoan'."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", no_accents.lower())


def rules_mention(rules_text: str, phrase: str | None) -> bool:
    return bool(phrase) and normalise(phrase) in normalise(rules_text)


_NUM = r"(-?\d+)"
_UNIT = r"\s*°?\s*([FC])"
_BELOW = re.compile(rf"^{_NUM}{_UNIT}\s+or\s+(?:below|lower|less)$", re.I)
_ABOVE = re.compile(rf"^{_NUM}{_UNIT}\s+or\s+(?:higher|above|more)$", re.I)
_RANGE = re.compile(rf"^{_NUM}\s*-\s*{_NUM}{_UNIT}$", re.I)
_SINGLE = re.compile(rf"^{_NUM}{_UNIT}$", re.I)


def parse_bucket(label: str) -> tuple[int | None, int | None, str]:
    """'58-59°F' → (58, 59, 'F');  '57°F or below' → (None, 57, 'F');
    '28°C' → (28, 28, 'C');  '37°C or higher' → (37, None, 'C')."""
    s = label.strip().replace("−", "-").replace("–", "-")
    if m := _BELOW.match(s):
        return None, int(m.group(1)), m.group(2).upper()
    if m := _ABOVE.match(s):
        return int(m.group(1)), None, m.group(2).upper()
    if m := _RANGE.match(s):
        return int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if m := _SINGLE.match(s):
        n = int(m.group(1))
        return n, n, m.group(2).upper()
    raise ValueError(f"Unrecognised bucket label: {label!r}")


def resolution_round(value: float) -> int:
    """Markets resolve on whole degrees; round half up (72.5 → 73)."""
    return int(floor(value + 0.5))


def _json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        return json.loads(v)
    return []


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class Bucket:
    city_slug: str
    target_date: date
    event_id: str
    event_slug: str
    market_id: str
    condition_id: str
    label: str
    lower: int | None
    upper: int | None
    unit: str
    token_yes: str
    token_no: str
    neg_risk: bool
    rules_ok: bool
    closed: bool

    def contains(self, value: float) -> bool:
        r = resolution_round(value)
        return (self.lower is None or r >= self.lower) and (self.upper is None or r <= self.upper)


@dataclass(frozen=True)
class CityDay:
    city_slug: str
    target_date: date
    event_slug: str
    found: bool
    buckets: tuple[Bucket, ...] = ()
    rules_ok: bool = False
    problem: str | None = None

    def bucket_for(self, value: float) -> Bucket | None:
        matches = [b for b in self.buckets if b.contains(value)]
        return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------- client
class GammaClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        r = await self._http.get(f"{GAMMA_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    async def fetch_city_day(
        self, *, city_slug: str, target_date: date, unit: str, rules_phrase: str | None
    ) -> CityDay:
        slug = event_slug(city_slug, target_date)
        try:
            events = await self._get("/events", {"slug": slug})
        except httpx.HTTPError as exc:
            return CityDay(city_slug, target_date, slug, found=False, problem=f"http error: {exc}")
        if not events:
            return CityDay(city_slug, target_date, slug, found=False, problem="no event listed")

        ev = events[0]
        neg_risk = bool(ev.get("negRisk"))
        buckets: list[Bucket] = []
        problems: list[str] = []
        all_rules_ok = True

        for m in ev.get("markets", []):
            label = m.get("groupItemTitle") or ""
            try:
                lo, hi, bucket_unit = parse_bucket(label)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if bucket_unit != unit:
                problems.append(f"unit mismatch on {label!r}: expected °{unit}")
                continue

            outcomes = [str(o).lower() for o in _json_list(m.get("outcomes"))]
            tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
            if len(tokens) != 2 or outcomes != ["yes", "no"]:
                problems.append(f"unexpected outcomes/tokens on {label!r}")
                continue

            ok = rules_mention(m.get("description", ""), rules_phrase)
            all_rules_ok &= ok
            buckets.append(Bucket(
                city_slug=city_slug, target_date=target_date,
                event_id=str(ev.get("id")), event_slug=slug,
                market_id=str(m.get("id")), condition_id=str(m.get("conditionId")),
                label=label, lower=lo, upper=hi, unit=bucket_unit,
                token_yes=tokens[0], token_no=tokens[1],
                neg_risk=neg_risk, rules_ok=ok, closed=bool(m.get("closed")),
            ))

        buckets.sort(key=lambda b: (b.lower is not None, b.lower if b.lower is not None else 0))
        if not buckets:
            problems.append("no parseable buckets")
        elif not all_rules_ok:
            problems.append("rules text does not name the catalogued station — NOT TRADEABLE")
        if not _covers_everything(buckets):
            problems.append("buckets have gaps or overlaps")

        return CityDay(
            city_slug, target_date, slug, found=True, buckets=tuple(buckets),
            rules_ok=bool(buckets) and all_rules_ok and _covers_everything(buckets),
            problem="; ".join(problems) or None,
        )

    async def search_city_slugs(self) -> set[str]:
        """Best-effort: city slugs that currently have an active highest-temperature event.
        Used only to warn about cities that are missing from the catalog."""
        slugs: set[str] = set()
        try:
            data = await self._get(
                "/public-search",
                {"q": "highest temperature", "limit_per_type": 100, "events_status": "active"},
            )
        except httpx.HTTPError:
            return slugs
        pat = re.compile(r"^highest-temperature-in-(.+)-on-[a-z]+-\d{1,2}-\d{4}$")
        for ev in data.get("events", []) or []:
            if m := pat.match(ev.get("slug", "")):
                slugs.add(m.group(1))
        return slugs


def _covers_everything(buckets: list[Bucket] | tuple[Bucket, ...]) -> bool:
    """Buckets must tile the number line exactly: open below, contiguous, open above."""
    if not buckets:
        return False
    if buckets[0].lower is not None or buckets[-1].upper is not None:
        return False
    for prev, nxt in zip(buckets, buckets[1:]):
        if prev.upper is None or nxt.lower is None or nxt.lower != prev.upper + 1:
            return False
    return True
