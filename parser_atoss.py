import re
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Any


# --- PDF text extraction helpers ---

# Suppress pdfminer warnings about FontBBox
logging.getLogger("pdfminer").setLevel(logging.ERROR)

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
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except Exception:
        return None
    try:
        lines = []
        for page_layout in extract_pages(str(path)):
            # Get all text elements
            elements = []
            for element in page_layout:
                if isinstance(element, LTTextContainer):
                    for text_line in element:
                        elements.append(text_line)
            
            # Sort by Y descending (top of page first), then X ascending
            elements.sort(key=lambda e: (-e.y1, e.x0))
            
            # Group by Y with some tolerance to reconstruct lines
            current_y = None
            current_line = []
            tolerance = 5 # points
            
            for e in elements:
                if current_y is None or abs(e.y1 - current_y) < tolerance:
                    current_line.append(e)
                    if current_y is None:
                        current_y = e.y1
                else:
                    # Flush current line
                    current_line.sort(key=lambda x: x.x0)
                    lines.append(" ".join(el.get_text().strip() for el in current_line))
                    current_line = [e]
                    current_y = e.y1
            
            if current_line:
                current_line.sort(key=lambda x: x.x0)
                lines.append(" ".join(el.get_text().strip() for el in current_line))
                
        return "\n".join(lines).strip()
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
            if start <= end:
                pairs.append((start, end))
            buf = []
    return pairs


def _detect_location(line: str) -> str:
    return "remote" if MA_TOKEN_RE.search(line) else ""


def parse_atoss_text(text: str) -> Tuple[List[Entry], Dict[str, str]]:
    entries: List[Entry] = []
    day_codes: Dict[str, str] = {}

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
    day_weekday: Optional[str] = None
    day_lines: List[str] = []

    def flush_day():
        nonlocal day_lines, day_dom, in_day, day_weekday
        if not in_day or day_dom is None or current_month is None or current_year is None:
            day_lines = []
            in_day = False
            day_dom = None
            return

        # Determine location:
        # Default to office. If we find 'ma', it's remote. 
        # If we find another code (token after the plan), use that code.
        loc = "office"
        if day_lines:
            # First line part after header usually looks like: [PLAN] [CODE] [TIMES]...
            # e.g. "748a ? 0:00" or "748a 08:30 12:00"
            parts = day_lines[0].split()
            if len(parts) >= 2:
                candidate = parts[1]
                # A code is a token that is not a time (no ':') 
                # and not a standalone number (totals/meta)
                if ":" not in candidate and not candidate.replace("-", "").isdigit():
                    if candidate.lower() == "ma":
                        loc = "remote"
                    else:
                        loc = candidate

                # NEW: Detect Holidays (Feiertage)
                # If target hours are 000 on a weekday, it's a holiday (unless another code like U/K is already set)
                if loc in ("office", "") and parts[0].startswith("000") and day_weekday in ("Mo", "Di", "Mi", "Do", "Fr"):
                    loc = "F"

        # Double check for 'ma' anywhere in the day's lines
        for ln in day_lines:
            if MA_TOKEN_RE.search(ln):
                loc = "remote"
                break

        # Time token extraction: Look for arrival/departure bits (k/a) or Dienstgang markers
        all_pairs = set()
        for i, ln in enumerate(day_lines):
            if BLOCK_END_RE.search(ln):
                continue
            
            # Find all times on this specific line
            line_times = TIME_RE.findall(ln)
            
            # If it's the first line, always consider its pairs (usually main work)
            if i == 0:
                for p in _pair_times(line_times):
                    all_pairs.add(p)
            
            # If the line has 'k', 'a', 'dg' or 'dienstgang', it's a marker line
            if re.search(r"\b[ka]\b|\bdg\b|dienstgang", ln, re.IGNORECASE):
                for p in _pair_times(line_times):
                    all_pairs.add(p)
            else:
                # Individual token check for k/a (in case they are mixed with other text)
                marked_tokens = []
                for m in re.finditer(r"(?:\bk\b\s*)?([01]?\d|2[0-3]):([0-5]\d)(?:\s*\b[k|a]\b)?", ln, re.IGNORECASE):
                    hh, mm = m.group(1), m.group(2)
                    span = m.span()
                    context = ln[max(0, span[0]-4): span[1]+4]
                    if re.search(r"\b[ka]\b", context, re.IGNORECASE):
                        marked_tokens.append((hh, mm))
                for p in _pair_times(marked_tokens):
                    all_pairs.add(p)

        pairs = sorted(list(all_pairs))



        r_start = time(6, 0)
        r_end = time(20, 0)
        def in_rahmen(p: Tuple[time, time]) -> bool:
            s, e = p
            return (s >= r_start) and (e <= r_end) and (e >= s)

        current_iso = date(current_year, current_month, day_dom).isoformat()
        
        for s, e in filter(in_rahmen, pairs):
            entries.append(Entry(
                source="work",
                date=current_iso,
                start=_fmt_time(s),
                end=_fmt_time(e),
                minutes=_minutes_between(s, e),
                location=loc,
            ))
        
        # Track the code even if no entries, unless it is default 'office'
        if loc != "office":
            day_codes[current_iso] = loc

        # Reset day state
        day_lines = []
        in_day = False
        day_dom = None
        day_weekday = None

    for raw in lines:
        mh = MONTH_HEADER_RE.search(raw)
        if mh:
            month_name, yr = mh.group(1), int(mh.group(2))
            current_month = month_map.get(month_name)
            current_year = yr

        dh = DAY_HEADER_RE.match(raw)
        if dh and current_month and current_year:
            flush_day()
            in_day = True
            day_weekday = dh.group(1)
            day_dom = int(dh.group(2))
            rest = raw[dh.end():].strip()
            day_lines = [rest] if rest else []
            continue

        if in_day:
            if DAY_HEADER_RE.match(raw) or DATE_RE.search(raw) or BLOCK_END_RE.search(raw):
                flush_day()
            else:
                day_lines.append(raw)

    flush_day()
    return entries, day_codes


def parse_atoss_pdf(path: Path) -> Dict[str, Any]:
    text = extract_text_for_debug(path)
    entries, day_codes = parse_atoss_text(text)
    return {
        "entries": [asdict(e) for e in entries],
        "day_codes": day_codes
    }
