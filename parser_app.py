import re
from dataclasses import dataclass, asdict
from datetime import date, time, datetime
from pathlib import Path
from typing import List, Tuple

from parser_atoss import extract_text_for_debug


@dataclass
class AppEntry:
    source: str  # "app"
    date: str    # YYYY-MM-DD
    start: str   # HH:MM
    end: str     # HH:MM
    minutes: int
    location: str  # default "remote" (treat as remote unless line says Norm)


TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
BILLING_RE = re.compile(r"Billing period:\s*(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})")
DAY_RE = re.compile(r"^(\d{2})\s+([A-Za-z]{3})\b")


def _fmt_time(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _minutes_between(h1: int, m1: int, h2: int, m2: int) -> int:
    a = datetime(2000, 1, 1, h1, m1)
    b = datetime(2000, 1, 1, h2, m2)
    return max(0, int((b - a).total_seconds() // 60))


def parse_app_text(text: str) -> List[AppEntry]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Determine month/year from billing period (use end date for safety)
    year = None
    month = None
    for ln in lines:
        m = BILLING_RE.search(ln)
        if m:
            # Use the end date's year/month (m.group 4..6)
            day2, mon2, yr2 = int(m.group(4)), int(m.group(5)), int(m.group(6))
            year, month = yr2, mon2
            break
    if not year or not month:
        # Fallback: try to find any dd.mm.yyyy and use its month/year
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if m:
            month, year = int(m.group(2)), int(m.group(3))

    entries: List[AppEntry] = []
    for ln in lines:
        dm = DAY_RE.match(ln)
        if not dm or not year or not month:
            continue
        dom = int(dm.group(1))
        # Extract first two times (start, end)
        times = TIME_RE.findall(ln)
        if len(times) < 2:
            continue
        (s_hh, s_mm), (e_hh, e_mm) = times[0], times[1]
        s_h, s_m = int(s_hh), int(s_mm)
        e_h, e_m = int(e_hh), int(e_mm)

        # (Removed filter that discards entries outside 06:00-20:00)

        d = date(year, month, dom).isoformat()
        minutes = _minutes_between(s_h, s_m, e_h, e_m)

        # Location: default unknown; 'Norm' => office; 'Mobil' => remote
        loc = ""
        low = ln.lower()
        if "norm" in low:
            loc = "office"
        elif "mobil" in low:
            loc = "remote"

        entries.append(AppEntry(
            source="app",
            date=d,
            start=_fmt_time(s_h, s_m),
            end=_fmt_time(e_h, e_m),
            minutes=minutes,
            location=loc,
        ))

    return entries


def parse_app_pdf(path: Path) -> List[dict]:
    text = extract_text_for_debug(path)
    return [asdict(e) for e in parse_app_text(text)]
