"""Synthetic month reports for demos and AI-assist testing.

The AI assistant is deliberately not allowed to invent worked hours, so test
fixtures are generated here instead: deterministic, seeded, and shaped like a
real month with a known mix of rule violations.
"""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

Location = str


@dataclass(frozen=True)
class Scenario:
    name: str
    weight: int
    code: str = ""
    note: str = ""


SCENARIOS: Tuple[Scenario, ...] = (
    Scenario("standard", 46),
    Scenario("target_azv", 10),
    Scenario("three_split", 8),
    Scenario("short_break", 8),
    Scenario("long_shift", 6),
    Scenario("early_start", 5),
    Scenario("friday_late", 5),
    Scenario("overlap", 5),
    Scenario("short_day", 4),
    Scenario("home_office_note", 3, note="Homeoffice, Nachmittagstermine"),
    Scenario("absence_vacation", 4, code="U"),
    Scenario("absence_sick", 3, code="K"),
    Scenario("absence_trip", 2, code="D"),
    Scenario("empty", 3),
)

MESSY_WEIGHTS = {
    "standard": 12,
    "target_azv": 3,
    "three_split": 5,
    "short_break": 18,
    "long_shift": 16,
    "early_start": 12,
    "friday_late": 12,
    "overlap": 12,
    "short_day": 6,
    "home_office_note": 2,
    "absence_vacation": 3,
    "absence_sick": 2,
    "absence_trip": 2,
    "empty": 8,
}

CLEAN_WEIGHTS = {
    "standard": 60,
    "target_azv": 20,
    "three_split": 10,
    "home_office_note": 5,
    "absence_vacation": 3,
    "absence_sick": 2,
}

LOCATIONS: Tuple[Location, ...] = ("remote", "remote", "office", "dg")


def _hm(total_minutes: int) -> str:
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _span(start_min: int, minutes: int) -> Tuple[int, int]:
    return start_min, start_min + minutes


def _plan_intervals(name: str, is_friday: bool) -> List[Tuple[int, int]]:
    """Return (start, end) minutes since midnight for a scenario."""
    if name == "standard":
        return [_span(8 * 60, 240), _span(12 * 60 + 30, 228)]  # 07:48 net, 30m break
    if name == "target_azv":
        return [_span(8 * 60, 240), _span(12 * 60 + 30, 252)]  # 08:12 net
    if name == "three_split":
        return [_span(8 * 60, 150), _span(11 * 60, 120), _span(14 * 60, 150)]
    if name == "short_break":
        return [_span(8 * 60, 240), _span(12 * 60 + 15, 255)]  # 15m break after 8h15
    if name == "long_shift":
        return [_span(7 * 60 + 30, 270), _span(12 * 60 + 30, 330)]  # 10:00 net, 30m break
    if name == "early_start":
        return [_span(5 * 60 + 40, 300), _span(11 * 60 + 10, 270)]
    if name == "friday_late":
        start = 10 * 60 if is_friday else 11 * 60
        return [_span(start, 270), _span(start + 300, 255)]
    if name == "overlap":
        return [_span(8 * 60, 240), _span(11 * 60 + 30, 300)]
    if name == "short_day":
        return [_span(9 * 60, 210)]
    if name == "home_office_note":
        return [_span(9 * 60, 240), _span(13 * 60 + 30, 228)]
    return []


def _pick_scenario(rng: random.Random, weights: Dict[str, int]) -> Scenario:
    names = [s.name for s in SCENARIOS]
    picks = [weights.get(name, 0) for name in names]
    if not any(picks):
        picks = [1 for _ in names]
    chosen = rng.choices(SCENARIOS, weights=picks, k=1)[0]
    return chosen


def _month_days(year_month: str) -> List[date]:
    year, month = (int(part) for part in year_month.split("-"))
    _, last_day = calendar.monthrange(year, month)
    return [date(year, month, day) for day in range(1, last_day + 1)]


def build_demo_month(
    year_month: str,
    seed: int = 7,
    profile: str = "mixed",
    through_today: bool = False,
) -> Dict[str, Any]:
    """Build synthetic entries, day codes and notes for one month."""
    if profile not in ("mixed", "clean", "messy"):
        raise ValueError(f"Unknown profile: {profile}")
    if profile == "clean":
        weights = CLEAN_WEIGHTS
    elif profile == "messy":
        weights = MESSY_WEIGHTS
    else:
        weights = {s.name: s.weight for s in SCENARIOS}

    rng = random.Random(f"{seed}:{year_month}:{profile}")
    today = date.today()

    entries: List[Dict[str, Any]] = []
    day_codes: Dict[str, str] = {}
    day_notes: Dict[str, str] = {}

    for day in _month_days(year_month):
        if day.weekday() >= 5:
            continue
        if through_today and day > today:
            break

        iso = day.isoformat()
        scenario = _pick_scenario(rng, weights)
        if scenario.code:
            day_codes[iso] = scenario.code
            if scenario.note:
                day_notes[iso] = scenario.note
            continue
        if scenario.name == "empty":
            continue
        if scenario.note:
            day_notes[iso] = scenario.note

        is_friday = day.weekday() == 4
        plan = _plan_intervals(scenario.name, is_friday)
        location = rng.choice(LOCATIONS)
        source = "app" if rng.random() > 0.25 else "manual"
        for start_min, end_min in plan:
            entries.append({
                "date": iso,
                "start": _hm(start_min),
                "end": _hm(end_min),
                "minutes": end_min - start_min,
                "location": location,
                "source": source,
            })

    entries.sort(key=lambda e: (e["date"], e["start"]))
    return {"entries": entries, "day_codes": day_codes, "day_notes": day_notes}


def demo_filename(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    return f"demo_{calendar.month_name[month].lower()}_{year % 100:02d}.txt"


def normalize_month(value: str) -> Optional[str]:
    """Accept 2026-11 or 2026-11-01 and return YYYY-MM."""
    parts = (value or "").strip().split("-")
    if len(parts) < 2:
        return None
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (1 <= month <= 12) or not (1900 <= year <= 2999):
        return None
    return f"{year:04d}-{month:02d}"
