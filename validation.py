from __future__ import annotations
import csv
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Any


# Types
Entry = Dict[str, Any]


def _parse_time(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(int(hh), int(mm))


def _minutes_between(a: time, b: time) -> int:
    da = datetime(2000, 1, 1, a.hour, a.minute)
    db = datetime(2000, 1, 1, b.hour, b.minute)
    return max(0, int((db - da).total_seconds() // 60))


def _norm_entry(e: Entry) -> Entry:
    # Ensure required keys and normalize values
    e = dict(e)
    e.setdefault("source", "")
    e.setdefault("date", "")
    e.setdefault("start", "")
    e.setdefault("end", "")
    e.setdefault("minutes", 0)
    e.setdefault("location", "")
    return e


def load_entries(files: List[Path]) -> List[Entry]:
    import json
    out: List[Entry] = []
    for p in files:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            out.extend(_norm_entry(x) for x in data if isinstance(x, dict))
    return out


def _make_id(date_s: str, start_s: str, end_s: str) -> str:
    return f"{date_s}_{start_s}-{end_s}"


def merge_entries(entries: List[Entry]) -> List[Entry]:
    # Dedupe by (date, start, end). Combine sources and choose location.
    merged: Dict[Tuple[str, str, str], Entry] = {}
    for e in entries:
        e = _norm_entry(e)
        key = (e["date"], e["start"], e["end"])
        # recompute minutes in case mismatch
        try:
            s = _parse_time(e["start"]) ; t = _parse_time(e["end"])
            e["minutes"] = _minutes_between(s, t)
        except Exception:
            pass

        if key not in merged:
            merged[key] = {
                "id": _make_id(e["date"], e["start"], e["end"]),
                "date": e["date"],
                "start": e["start"],
                "end": e["end"],
                "minutes": e.get("minutes", 0),
                "location": e.get("location", ""),
                "sources": [e.get("source", "")] if e.get("source") else [],
            }
        else:
            m = merged[key]
            # append source if new
            src = e.get("source", "")
            if src and src not in m["sources"]:
                m["sources"].append(src)
            # location preference: prefer 'work' entry's location; otherwise if any 'remote'
            mloc = m.get("location") or ""
            eloc = e.get("location") or ""
            # If current is empty and new has value, take it
            if not mloc and eloc:
                m["location"] = eloc
            # Prefer work over app
            elif src == "work":
                m["location"] = eloc or mloc
            # Otherwise, if any says remote, set remote
            elif eloc == "remote":
                m["location"] = "remote"
            # minutes should match; keep existing

    # Make location defaults explicit
    for m in merged.values():
        if not m.get("location"):
            m["location"] = "office"
    # Sort
    return sorted(merged.values(), key=lambda r: (r["date"], r["start"], r["end"]))


def _day_key(e: Entry) -> Tuple[str, str]:
    return (e["date"],)


def _overlap(a: Entry, b: Entry) -> int:
    sa, ea = _parse_time(a["start"]), _parse_time(a["end"])
    sb, eb = _parse_time(b["start"]), _parse_time(b["end"])
    start = max(sa, sb)
    end = min(ea, eb)
    return max(0, _minutes_between(start, end))


def validate(entries_merged: List[Entry]) -> Dict[str, Any]:
    # Group by date
    by_day: Dict[str, List[Entry]] = {}
    for e in entries_merged:
        by_day.setdefault(e["date"], []).append(e)

    issues: List[Dict[str, Any]] = []
    days: List[Dict[str, Any]] = []

    R_START = time(6, 0)
    R_END = time(20, 0)
    S_MIN = 7 * 60 + 48  # 7:48
    D_MAX = 10 * 60       # 10:00

    for d, items in sorted(by_day.items()):
        # Sort intervals by start
        items.sort(key=lambda r: (r["start"], r["end"]))

        # Rahmenzeit + compute gross minutes
        gross = 0
        for e in items:
            s, e_t = _parse_time(e["start"]), _parse_time(e["end"])
            if not (s >= R_START and e_t <= R_END):
                issues.append({
                    "type": "outside_rahmenzeit",
                    "date": d,
                    "id": e.get("id"),
                    "start": e["start"],
                    "end": e["end"],
                    "message": "Interval outside 06:00–20:00",
                })
            gross += _minutes_between(s, e_t)

        # Break minutes = sum gaps between consecutive intervals
        break_min = 0
        for i in range(1, len(items)):
            prev = items[i-1]
            curr = items[i]
            end_prev = _parse_time(prev["end"])
            start_curr = _parse_time(curr["start"])
            gap = _minutes_between(end_prev, start_curr)
            if gap > 0:
                break_min += gap

        # Required break threshold based on gross
        required_break = 0
        if gross > (6 * 60 + 30):
            required_break = 30
        if gross > (9 * 60 + 45):
            required_break = 45

        if break_min < required_break:
            issues.append({
                "type": "insufficient_breaks",
                "date": d,
                "required": required_break,
                "actual": break_min,
                "missing": required_break - break_min,
                "message": "Breaks below legal minimum",
            })

        # Overlaps: any overlap > 0 between any pair
        for i in range(len(items)):
            for j in range(i+1, len(items)):
                a, b = items[i], items[j]
                ov = _overlap(a, b)
                if ov > 0:
                    # Determine cross-sources
                    sa = set(a.get("sources", []) or [])
                    sb = set(b.get("sources", []) or [])
                    overlap_type = "overlap_cross_sources" if not sa.isdisjoint(sb) is False else "overlap_within_source"
                    # Actually, if sources sets have any intersection -> within; else cross
                    overlap_type = "overlap_within_source" if (sa & sb) else "overlap_cross_sources"
                    issues.append({
                        "type": overlap_type,
                        "date": d,
                        "a_id": a.get("id"),
                        "a": {"start": a["start"], "end": a["end"], "sources": list(sa) or None},
                        "b_id": b.get("id"),
                        "b": {"start": b["start"], "end": b["end"], "sources": list(sb) or None},
                        "minutes": ov,
                        "message": "Intervals overlap",
                    })

        # Daily max
        if gross > D_MAX:
            issues.append({
                "type": "over_daily_max",
                "date": d,
                "gross_minutes": gross,
                "max_minutes": D_MAX,
                "excess": gross - D_MAX,
                "message": "Worked time exceeds 10h",
            })

        days.append({
            "date": d,
            "totals": {
                "gross_minutes": gross,
                "break_minutes": break_min,
                "net_minutes": gross,  # net excludes breaks from entries, so same as gross
                "soll_minutes": S_MIN,
                "soll_delta_minutes": gross - S_MIN,
            },
        })

    return {"entries": entries_merged, "days": days, "issues": issues}


def write_issues_csv(issues: List[Dict[str, Any]], out: Path) -> None:
    if not issues:
        out.write_text("type,date,message\n", encoding="utf-8")
        return
    # Flatten nested a/b first to know all keys
    flat_rows: List[Dict[str, Any]] = []
    for it in issues:
        row = dict(it)
        for side in ("a", "b"):
            if side in row and isinstance(row[side], dict):
                for kk, vv in row[side].items():
                    row[f"{side}_{kk}"] = vv
                del row[side]
        flat_rows.append(row)
    # Collect keys
    keys = set()
    for row in flat_rows:
        keys.update(row.keys())
    # Ensure some ordering
    preferred = ["type", "date", "start", "end", "message", "minutes", "required", "actual", "missing", "gross_minutes", "max_minutes", "excess",
                 "a_start", "a_end", "a_sources", "b_start", "b_end", "b_sources"]
    header = [k for k in preferred if k in keys] + [k for k in sorted(keys) if k not in preferred]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(flat_rows)
