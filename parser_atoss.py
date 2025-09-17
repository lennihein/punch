import re
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# --- PDF text extraction helpers ---

def _try_extract_text_pypdf(path: Path) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        parts: List[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return None


def _try_extract_text_pdfminer(path: Path) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:
        return None
    try:
        return extract_text(str(path))
    except Exception:
        return None


def extract_text_for_debug(path: Path) -> str:
    text = _try_extract_text_pypdf(path) or _try_extract_text_pdfminer(path)
    if not text:
        raise RuntimeError("Failed to extract text. Install dependencies from requirements.txt")
    return text


# --- Data model ---

@dataclass
class Entry:
    source: str  # "work"
    date: str    # YYYY-MM-DD
    start: str   # HH:MM
    end: str     # HH:MM
    minutes: int
    location: str  # "remote" | "office"


# --- Parsing ---

DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?!\d)")
TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
MA_TOKEN_RE = re.compile(r"\bma\b", re.IGNORECASE)
DAY_HEADER_RE = re.compile(r"^(Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{2})\b")
MONTH_HEADER_RE = re.compile(
    r"\b(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b"
)
BLOCK_END_RE = re.compile(r"Zwischensalden|Neue Startwerte|Schlusssalden|Gedruckt|Seite\b")


def _norm_date(d: Tuple[str, str, str]) -> str:
    day, mon, yr = (int(d[0]), int(d[1]), int(d[2]))
    if yr < 100:
        yr += 2000
    return date(yr, mon, day).isoformat()


def _to_time(hh: str, mm: str) -> time:
    return time(int(hh), int(mm))


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _minutes_between(a: time, b: time) -> int:
    dt = datetime.combine(date(2000, 1, 1), b) - datetime.combine(date(2000, 1, 1), a)
    return max(0, int(dt.total_seconds() // 60))


def _pair_times(times: List[Tuple[str, str]]) -> List[Tuple[time, time]]:
    # Given a list of (hh,mm) tokens in order, pair sequentially (0-1, 2-3, ...)
    pairs: List[Tuple[time, time]] = []
    buf: List[time] = []
    for hh, mm in times:
        buf.append(_to_time(hh, mm))
        if len(buf) == 2:
            start, end = buf
            if start < end:
                pairs.append((start, end))
            buf = []
    return pairs


def _detect_location(line: str) -> str:
    return "remote" if MA_TOKEN_RE.search(line) else "office"


def parse_atoss_text(text: str) -> List[Entry]:
    entries: List[Entry] = []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Track current month/year from header
    month_map = {
        "Januar": 1, "February": 2, "Februar": 2, "März": 3, "March": 3,
        "April": 4, "Mai": 5, "May": 5, "Juni": 6, "June": 6,
        "Juli": 7, "July": 7, "August": 8, "September": 9,
        "Oktober": 10, "October": 10, "November": 11, "Dezember": 12, "December": 12,
    }
    current_month: Optional[int] = None
    current_year: Optional[int] = None

    in_day = False
    day_dom: Optional[int] = None
    day_lines: List[str] = []

    def flush_day():
        nonlocal day_lines, day_dom, in_day
        if not in_day or day_dom is None or current_month is None or current_year is None:
            day_lines = []
            in_day = False
            day_dom = None
            return

        # Determine location: if any line has 'ma' => remote
        loc = "office"
        for ln in day_lines:
            if MA_TOKEN_RE.search(ln):
                loc = "remote"
                break

        # Time token extraction
        # Prefer tokens that are adjacent to 'k'/'a'; otherwise fall back to first two times on the day header line.
        # Collect all time tokens in order across day lines until a totals-like field starts.
        time_tokens: List[Tuple[str, str]] = []
        saw_ka = False
        for ln in day_lines:
            # If line looks like a totals/meta line, skip
            if BLOCK_END_RE.search(ln):
                continue
            # Extract pairs that have k/a around them
            # pattern: (k )?HH:MM (k|a)?
            for m in re.finditer(r"(?:\bk\b\s*)?([01]?\d|2[0-3]):([0-5]\d)(?:\s*\b[k|a]\b)?", ln):
                hh, mm = m.group(1), m.group(2)
                # Heuristic: accept only if near a k/a token in the same line
                span = m.span()
                context = ln[max(0, span[0]-4): span[1]+4]
                if re.search(r"\b[ka]\b", context):
                    time_tokens.append((hh, mm))
                    saw_ka = True
            # If no k/a at all, we will fall back after loop

        if not saw_ka:
            # Fallback: take the first two time tokens from the first day line
            if day_lines:
                tkns = TIME_RE.findall(day_lines[0])
                # Some lines include totals after the first two tokens; limit to first two
                tkns = tkns[:2]
                time_tokens.extend(tkns)

        pairs = _pair_times(time_tokens)

        # Filter pairs by Rahmenzeit 06:00–20:00
        r_start = time(6, 0)
        r_end = time(20, 0)
        def in_rahmen(p: Tuple[time, time]) -> bool:
            s, e = p
            return (s >= r_start) and (e <= r_end) and (e > s)

        for s, e in filter(in_rahmen, pairs):
            d = date(current_year, current_month, day_dom).isoformat()
            entries.append(Entry(
                source="work",
                date=d,
                start=_fmt_time(s),
                end=_fmt_time(e),
                minutes=_minutes_between(s, e),
                location=loc,
            ))

        # Reset day state
        day_lines = []
        in_day = False
        day_dom = None

    for raw in lines:
        # Update month/year if header present
        mh = MONTH_HEADER_RE.search(raw)
        if mh:
            month_name, yr = mh.group(1), int(mh.group(2))
            current_month = month_map.get(month_name)
            current_year = yr

        # Start of a new day block
        dh = DAY_HEADER_RE.match(raw)
        if dh and current_month and current_year:
            # flush previous day
            flush_day()
            in_day = True
            day_dom = int(dh.group(2))
            # store line without the leading day header tokens
            rest = raw[dh.end():].strip()
            day_lines = [rest] if rest else []
            continue

        # If inside a day, collect continuation lines until a block end or next day
        if in_day:
            if DAY_HEADER_RE.match(raw) or DATE_RE.search(raw) or BLOCK_END_RE.search(raw):
                flush_day()
                # Re-process this line in outer loop (could be a new day)
                # simple approach: treat as not in_day and continue normal loop handling
            else:
                day_lines.append(raw)

    # flush last day at EOF
    flush_day()

    return entries


def parse_atoss_pdf(path: Path) -> List[dict]:
    text = extract_text_for_debug(path)
    entries = parse_atoss_text(text)
    return [asdict(e) for e in entries]
