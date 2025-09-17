import json
import re
from collections import defaultdict
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from validation import _make_id, _minutes_between, _parse_time

TARGET_MINUTES_PER_DAY = 468
DEFAULT_COLUMNS: Sequence[str] = ("start", "end", "duration", "location", "source", "issues")
MINIMAL_COLUMNS: Sequence[str] = ("start", "end", "duration", "issues")


def _load_context(files: List[Path]) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    days: Dict[str, Dict[str, Any]] = {}

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_entries = data.get("entries") or []
            raw_issues = data.get("issues") or []
            raw_days = data.get("days") or []
        elif isinstance(data, list):
            raw_entries = data
            raw_issues = []
            raw_days = []
        else:
            continue

        for item in raw_entries:
            if isinstance(item, dict):
                entries.append(dict(item))
        for issue in raw_issues:
            if isinstance(issue, dict):
                issues.append(dict(issue))
        for day in raw_days:
            if isinstance(day, dict) and day.get("date"):
                days[day["date"]] = dict(day)

    entries.sort(key=lambda r: (r.get("date", ""), r.get("start", ""), r.get("end", "")))
    return {"entries": entries, "issues": issues, "days": list(days.values())}


def _format_minutes(total_minutes: int, pad_hour: bool = True) -> str:
    minutes = int(total_minutes or 0)
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    hours, mins = divmod(minutes, 60)
    if pad_hour:
        return f"{sign}{hours:02d}:{mins:02d}"
    return f"{sign}{hours}:{mins:02d}"


def _month_label(entries: Iterable[Dict[str, Any]]) -> str:
    for entry in entries:
        date_s = entry.get("date")
        try:
            dt = datetime.strptime(str(date_s), "%Y-%m-%d")
            return dt.strftime("%B %Y")
        except Exception:
            continue
    return "Unknown month"


def _weekday_label(date_s: str) -> str:
    try:
        dt = datetime.strptime(date_s, "%Y-%m-%d")
        return dt.strftime("%A")
    except Exception:
        return ""


def _entry_issue_tags(entry: Dict[str, Any], issues: Iterable[Dict[str, Any]]) -> str:
    tags: List[str] = []
    eid = entry.get("id")
    for issue in issues:
        itype = issue.get("type")
        if itype in ("outside_rahmenzeit",):
            if issue.get("id") == eid:
                tags.append(itype)
        elif isinstance(itype, str) and itype.startswith("overlap_"):
            if issue.get("a_id") == eid or issue.get("b_id") == eid:
                tags.append(itype)
    return ",".join(sorted(set(tags)))


def _day_issue_tags(date_s: str, issues: Iterable[Dict[str, Any]]) -> str:
    tags: List[str] = []
    for issue in issues:
        if issue.get("date") != date_s:
            continue
        itype = issue.get("type")
        if itype in ("insufficient_breaks", "over_daily_max"):
            tags.append(itype)
    return ",".join(sorted(set(tags)))


def _format_issue_summary(issue: Dict[str, Any]) -> str:
    typ = (issue.get("type") or "issue").replace("_", " ")
    date_s = issue.get("date")
    prefix = f"{date_s}: " if date_s else ""

    if issue.get("type") == "insufficient_breaks":
        required = issue.get("required")
        actual = issue.get("actual")
        missing = issue.get("missing")
        missing_part = f", missing {missing} min" if missing is not None else ""
        return f"{prefix}insufficient breaks (required {required} min, actual {actual} min{missing_part})"

    if issue.get("type") == "over_daily_max":
        gross = issue.get("gross_minutes")
        max_minutes = issue.get("max_minutes")
        excess = issue.get("excess")
        details: List[str] = []
        if gross is not None and max_minutes is not None:
            details.append(f"{gross} min vs max {max_minutes} min")
        if excess is not None:
            details.append(f"excess {excess} min")
        detail_str = ", ".join(details)
        return f"{prefix}worked over daily max ({detail_str})" if detail_str else f"{prefix}worked over daily max"

    if issue.get("type") == "outside_rahmenzeit":
        start = issue.get("start")
        end = issue.get("end")
        return f"{prefix}interval {start}-{end} outside Rahmenarbeitszeit"

    if isinstance(issue.get("type"), str) and issue.get("type", "").startswith("overlap_"):
        a_id = issue.get("a_id") or "A"
        b_id = issue.get("b_id") or "B"
        minutes = issue.get("minutes")
        return f"{prefix}overlap between {a_id} and {b_id} ({minutes} min)"

    return f"{prefix}{typ}"


def summarize_issues(issues: Iterable[Dict[str, Any]]) -> List[str]:
    issue_dicts = [dict(it) for it in issues if isinstance(it, dict)]
    sorted_issues = sorted(issue_dicts, key=lambda x: (x.get("date", ""), x.get("type", "")))
    return [_format_issue_summary(it) for it in sorted_issues]


def find_empty_workdays(entries: Iterable[Dict[str, Any]], days: Iterable[Dict[str, Any]]) -> List[str]:
    entry_date_strings: List[str] = []
    range_date_strings: List[str] = []

    for e in entries:
        if not isinstance(e, dict):
            continue
        date_s = e.get("date")
        if date_s:
            entry_date_strings.append(str(date_s))
            range_date_strings.append(str(date_s))

    for d in days:
        if not isinstance(d, dict):
            continue
        date_s = d.get("date")
        if date_s:
            range_date_strings.append(str(date_s))

    range_dates: List[_date] = []
    for ds in range_date_strings:
        try:
            range_dates.append(datetime.strptime(ds, "%Y-%m-%d").date())
        except Exception:
            continue

    if not range_dates:
        return []

    min_date = min(range_dates)
    max_date = max(range_dates)
    have_entries = {
        datetime.strptime(ds, "%Y-%m-%d").date()
        for ds in entry_date_strings
        if isinstance(ds, str)
    }

    warnings: List[str] = []
    cur = min_date
    while cur <= max_date:
        if cur.weekday() < 5 and cur not in have_entries:
            warnings.append(cur.strftime("%Y-%m-%d (%A)"))
        cur += timedelta(days=1)

    return warnings


def render_report(entries: Iterable[Dict[str, Any]],
                  issues: Iterable[Dict[str, Any]],
                  days: Iterable[Dict[str, Any]],
                  *,
                  columns: Sequence[str] = DEFAULT_COLUMNS) -> str:
    entries_list = [dict(e) for e in entries]
    if not entries_list:
        return "No entries available."

    issues_list = [dict(i) for i in issues]
    days_map: Dict[str, Dict[str, Any]] = {}
    for day in days or []:
        if not isinstance(day, dict):
            continue
        date_s = day.get("date")
        if not date_s:
            continue
        days_map[date_s] = dict(day)

    month_label = _month_label(entries_list)
    today_str = _date.today().isoformat()

    entries_by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries_list:
        date_s = entry.get("date")
        if not date_s:
            continue
        entries_by_day[date_s].append(entry)

    day_summaries: Dict[str, Dict[str, int]] = {}
    for date_s, day in days_map.items():
        totals = day.get("totals") or {}
        net = int(totals.get("net_minutes", totals.get("gross_minutes", 0)) or 0)
        delta = totals.get("soll_delta_minutes")
        if delta is None:
            delta = totals.get("delta_minutes")
        if delta is None:
            delta = net - TARGET_MINUTES_PER_DAY
        day_summaries[date_s] = {
            "net_minutes": int(net),
            "delta_minutes": int(delta),
        }

    for date_s, rows in entries_by_day.items():
        if date_s in day_summaries:
            continue
        total = 0
        for r in rows:
            minutes = r.get("minutes")
            if not minutes:
                try:
                    minutes = _minutes_between(_parse_time(r.get("start", "")), _parse_time(r.get("end", "")))
                except Exception:
                    minutes = 0
            total += int(minutes or 0)
        day_summaries[date_s] = {
            "net_minutes": total,
            "delta_minutes": total - TARGET_MINUTES_PER_DAY,
        }

    monthly_delta = sum(summary["delta_minutes"] for summary in day_summaries.values())

    column_names = list(columns)
    issues_iter = list(issues_list)

    lines: List[str] = []
    lines.append(f"Month: {month_label}")
    lines.append(f"Generated: {today_str}")
    lines.append(f"Monthly delta vs 07:48/day: {_format_minutes(monthly_delta)}")
    lines.append("")

    issue_summaries = summarize_issues(issues_iter)
    if issue_summaries:
        lines.append("Issues:")
        for summary in issue_summaries:
            lines.append(f"- {summary}")
    else:
        lines.append("Issues: none")
    lines.append("")

    empty_workdays = find_empty_workdays(entries_list, days_map.values())
    if empty_workdays:
        lines.append("Empty workdays without entries:")
        for label in empty_workdays:
            lines.append(f"- {label}")
    else:
        lines.append("Empty workdays without entries: none")
    lines.append("")

    for date_s in sorted(entries_by_day.keys()):
        weekday = _weekday_label(date_s)
        summary = day_summaries.get(date_s, {"net_minutes": 0, "delta_minutes": 0})
        day_total = _format_minutes(summary["net_minutes"])
        day_delta = _format_minutes(summary["delta_minutes"])
        day_issue_text = _day_issue_tags(date_s, issues_iter)

        lines.append(f"{date_s} ({weekday})")
        lines.append(f"  Total worked: {day_total} (delta {day_delta})")
        if day_issue_text:
            lines.append(f"  Day issues: {day_issue_text}")

        rows: List[Dict[str, Any]] = []
        for entry in entries_by_day[date_s]:
            minutes = entry.get("minutes")
            if not minutes:
                try:
                    minutes = _minutes_between(_parse_time(entry.get("start", "")), _parse_time(entry.get("end", "")))
                except Exception:
                    minutes = 0
            sources = entry.get("sources")
            if isinstance(sources, list):
                source_label = ",".join(s for s in sources if s)
            else:
                source_label = str(entry.get("source", ""))
            base_row = {
                "start": entry.get("start", ""),
                "end": entry.get("end", ""),
                "duration": _format_minutes(int(minutes or 0)),
                "location": entry.get("location", ""),
                "source": source_label,
                "issues": _entry_issue_tags(entry, issues_iter),
            }
            rows.append({name: base_row.get(name, "") for name in column_names})

        widths = {name: len(name) for name in column_names}
        for row in rows:
            for name in column_names:
                widths[name] = max(widths[name], len(str(row.get(name, ""))))

        def fmt(values: Dict[str, Any]) -> str:
            parts: List[str] = []
            for name in column_names:
                parts.append(str(values.get(name, "")).ljust(widths[name]))
            return "  ".join(parts)

        header_row = {name: name for name in column_names}
        lines.append("    " + fmt(header_row))
        lines.append("    " + "  ".join("-" * widths[name] for name in column_names))
        for row in rows:
            lines.append("    " + fmt(row))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_monthly_report(files: List[Path]) -> str:
    ctx = _load_context(files)
    return render_report(ctx["entries"], ctx["issues"], ctx["days"], columns=DEFAULT_COLUMNS)


def render_review_report(result: Dict[str, Any]) -> str:
    entries = result.get("entries", []) or []
    issues = result.get("issues", []) or []
    days = result.get("days", []) or []
    return render_report(entries, issues, days, columns=DEFAULT_COLUMNS)


DAY_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
ENTRY_LINE_RE = re.compile(r"^\s{2,}(\d{2}:\d{2})\s+(\d{2}:\d{2})")


def parse_report_entries(path: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    current_date: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Month:") or stripped.startswith("Generated:") or stripped.startswith("Monthly delta"):
            continue
        if stripped.startswith("Total worked:") or stripped.startswith("Day issues:"):
            continue
        if stripped.lower().startswith("start"):
            continue
        if set(stripped.replace(' ', '')) == {'-'}:
            continue

        day_match = DAY_LINE_RE.match(stripped)
        if day_match and "(" in stripped:
            current_date = day_match.group(1)
            continue

        entry_match = ENTRY_LINE_RE.match(line)
        if entry_match and current_date:
            start, end = entry_match.groups()
            try:
                minutes = _minutes_between(_parse_time(start), _parse_time(end))
            except Exception:
                continue
            if minutes <= 0:
                continue
            entries.append({
                "id": _make_id(current_date, start, end),
                "date": current_date,
                "start": start,
                "end": end,
                "minutes": minutes,
                "location": "",
                "source": "manual",
            })

    return entries


def render_reviewed_report(result: Dict[str, Any]) -> str:
    entries = result.get("entries", []) or []
    issues = result.get("issues", []) or []
    days = result.get("days", []) or []
    return render_report(entries, issues, days, columns=MINIMAL_COLUMNS)
