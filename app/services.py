from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
import httpx

from parser_app import parse_app_pdf
from parser_atoss import parse_atoss_pdf
from table_utils import (
    GERMAN_MONTH_NAMES,
    GERMAN_WEEKDAYS,
    STATUS_MAP,
    build_month_pdf_data,
    compile_typst_pdf,
    parse_report_entries,
)
from validation import (
    D_MAX_BEAMTER_BRUTTO,
    D_MAX_TARIF,
    S_MIN,
    S_MIN_TARIF,
    VERBEAMTUNG_DATE,
    validate as run_validation,
)

ABSENCE_CODES = ("K", "S", "U", "F", "ZA", "D", "D1", "D2")
ZERO_SOLL_CODES = ("K", "S", "U", "F", "D", "D1", "D2")
VALID_DAY_CODES = set(ABSENCE_CODES)
R_START_MIN = 6 * 60

ISSUE_LABELS = {
    "insufficient_breaks": "Missing Break",
    "outside_rahmenzeit": "Outside Rahmenzeit",
    "overlap_cross_sources": "Source Overlap",
    "overlap_within_source": "Time Overlap",
    "over_daily_max": "Over Daily Max",
    "unclosed_punch": "Unclosed Punch",
    "unknown_location": "Missing Location",
    "short_weekday_no_code": "Short Day (<4h)",
    "atoss_mismatch": "ATOSS Differs",
    "empty_workday": "Missing Workday",
}

OP_LABELS = {
    "split_break": "Insert Break",
    "replace_intervals": "Adjust Hours",
    "update_interval": "Update Interval",
    "add_interval": "Add Interval",
    "delete_interval": "Delete Interval",
    "replace_day": "Replace Day",
    "set_day_code": "Set Absence Code",
}

UI_STATUS_MAP = {
    "U": "Urlaub",
    "ZA": "Zeitausgleich",
    "K": "Krank",
    "F": "Feiertag",
    "D": "Dienstreise",
    "D1": "Dienstreise (D1)",
    "D2": "Dienstreise (D2)",
    "S": "Sonderurlaub",
}


class AIConnectionError(Exception):
    """Raised when AI Assist is invoked without a valid connection or API response."""


def user_now(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name or "Europe/Berlin")
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    return datetime.now(tz)


def normalize_hm(raw: str) -> str:
    clean = (raw or "").strip()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", clean)
    if not m:
        raise HTTPException(status_code=400, detail=f"Invalid time format '{raw}', expected HH:MM")
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


def normalize_date(raw: str) -> str:
    clean = (raw or "").strip()
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date '{raw}', expected YYYY-MM-DD") from exc


def normalize_ym(raw: str) -> str:
    clean = (raw or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", clean):
        raise HTTPException(status_code=400, detail=f"Invalid year_month '{raw}', expected YYYY-MM")
    return clean


def hm_to_minutes(hm: str) -> int:
    hh, mm = hm.split(":")
    return int(hh) * 60 + int(mm)


def minutes_to_hm(total: int) -> str:
    clamped = max(0, min(23 * 60 + 59, int(total)))
    return f"{clamped // 60:02d}:{clamped % 60:02d}"


def format_minutes(total_minutes: int, signed: bool = False) -> str:
    val = int(total_minutes or 0)
    sign = "-" if val < 0 else ("+" if signed and val > 0 else "")
    aval = abs(val)
    return f"{sign}{aval // 60:02d}:{aval % 60:02d}"


SEPTEMBER_CAP_MINUTES = 40 * 60


def parse_signed_hm(raw: str) -> int:
    clean = (raw or "").strip()
    m = re.fullmatch(r"([+-])?(\d{1,4}):([0-5]\d)", clean)
    if not m:
        raise HTTPException(status_code=400, detail=f"Invalid balance format '{raw}', expected ±HH:MM (e.g. +40:00 or -05:30)")
    sign = -1 if m.group(1) == "-" else 1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))


def compute_overall_balance(
    db: sqlite3.Connection,
    user: Dict[str, Any],
    through_ym: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    raw_anchor_date = (user.get("balance_anchor_date") or "").strip()
    raw_anchor_min = user.get("balance_anchor_minutes")
    if not raw_anchor_date or raw_anchor_min is None:
        return None
    try:
        anchor_d = date.fromisoformat(raw_anchor_date)
        anchor_min = int(raw_anchor_min)
    except (ValueError, TypeError):
        return None

    now_dt = user_now(user["timezone"])
    today_d = now_dt.date()
    current_ym = now_dt.strftime("%Y-%m")

    if through_ym:
        ym = normalize_ym(through_ym)
        y, m = map(int, ym.split("-"))
        target_first_d = date(y, m, 1)
        if ym < current_ym:
            end_d = date(y, m, calendar.monthrange(y, m)[1])
        elif ym == current_ym:
            end_d = today_d
        else:
            return None
    else:
        target_first_d = date(today_d.year, today_d.month, 1)
        end_d = today_d

    if end_d < anchor_d:
        return None

    entry_rows = db.execute(
        """
        SELECT work_date, start_time, end_time
        FROM entries
        WHERE user_id = ? AND work_date >= ? AND work_date <= ?
          AND end_time IS NOT NULL AND deleted_at IS NULL
        """,
        (user["id"], anchor_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    worked_by_day: Dict[str, int] = {}
    for r in entry_rows:
        mins = max(0, hm_to_minutes(r["end_time"]) - hm_to_minutes(r["start_time"]))
        worked_by_day[r["work_date"]] = worked_by_day.get(r["work_date"], 0) + mins

    is_current_view = (end_d == today_d)
    if is_current_view:
        active_row = _get_active_entry(db, user["id"])
        if active_row is not None and active_row["work_date"] == today_d.isoformat():
            now_min = now_dt.hour * 60 + now_dt.minute
            elapsed = max(0, now_min - hm_to_minutes(active_row["start_time"]))
            worked_by_day[today_d.isoformat()] = worked_by_day.get(today_d.isoformat(), 0) + elapsed

    meta_rows = db.execute(
        """
        SELECT work_date, day_code
        FROM day_meta
        WHERE user_id = ? AND work_date >= ? AND work_date <= ? AND day_code IS NOT NULL
        """,
        (user["id"], anchor_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    codes_by_day = {r["work_date"]: (r["day_code"] or "").upper() for r in meta_rows}

    target_min = int(user["target_minutes"])
    user_verb_date = (user.get("verbeamtung_date") or "").strip() or None
    saldo = anchor_min
    month_start_saldo: Optional[int] = None
    yesterday_saldo: Optional[int] = None
    september_capped_minutes = 0

    cur = anchor_d
    while cur <= end_d:
        if cur == target_first_d:
            month_start_saldo = saldo
        if is_current_view and cur == today_d and anchor_d < today_d:
            yesterday_saldo = saldo
        iso = cur.isoformat()
        is_weekend = cur.weekday() >= 5
        code = codes_by_day.get(iso, "")
        soll = 0 if (is_weekend or code in ZERO_SOLL_CODES) else day_target_for_user(iso, target_min, user_verb_date)
        saldo += worked_by_day.get(iso, 0) - soll

        if cur.month == 9 and cur.day == 30 and saldo > SEPTEMBER_CAP_MINUTES:
            capped_here = saldo - SEPTEMBER_CAP_MINUTES
            saldo = SEPTEMBER_CAP_MINUTES
            if cur.year == end_d.year and end_d.month == 9:
                september_capped_minutes = capped_here

        cur += timedelta(days=1)

    return {
        "anchor_date": anchor_d.isoformat(),
        "anchor_minutes": anchor_min,
        "anchor_fmt": format_minutes(anchor_min, signed=True),
        "through_date": end_d.isoformat(),
        "start_minutes": month_start_saldo,
        "start_fmt": format_minutes(month_start_saldo, signed=True) if month_start_saldo is not None else None,
        "yesterday_minutes": yesterday_saldo,
        "yesterday_fmt": format_minutes(yesterday_saldo, signed=True) if yesterday_saldo is not None else None,
        "total_minutes": saldo,
        "total_fmt": format_minutes(saldo, signed=True),
        "september_capped_minutes": september_capped_minutes,
        "september_capped_fmt": format_minutes(september_capped_minutes) if september_capped_minutes > 0 else None,
    }


def rahmenzeit_end_min(d: date) -> int:
    return 18 * 60 if d.weekday() == 4 else 20 * 60


def day_target_for_user(
    date_iso: str,
    user_target_minutes: int,
    verbeamtung_date: Optional[str] = VERBEAMTUNG_DATE,
) -> int:
    if verbeamtung_date and date_iso < verbeamtung_date and user_target_minutes == S_MIN:
        return S_MIN_TARIF
    return user_target_minutes


def parse_client_ts(client_ts: Optional[str], tz_name: str) -> datetime:
    now_dt = user_now(tz_name)
    if not client_ts:
        return now_dt
    try:
        tz = now_dt.tzinfo
        parsed = datetime.fromisoformat(client_ts.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        else:
            parsed = parsed.astimezone(tz)
        if abs((parsed - now_dt).total_seconds()) <= 7 * 24 * 3600:
            return parsed
    except Exception:
        pass
    return now_dt


def _row_to_entry(row: sqlite3.Row) -> Dict[str, Any]:
    sources = json.loads(row["sources_json"] or "[]")
    end_time = row["end_time"]
    minutes = 0
    if end_time:
        minutes = max(0, hm_to_minutes(end_time) - hm_to_minutes(row["start_time"]))
    return {
        "id": int(row["id"]),
        "work_date": row["work_date"],
        "date": row["work_date"],
        "start": row["start_time"],
        "start_time": row["start_time"],
        "end": end_time,
        "end_time": end_time,
        "minutes": minutes,
        "location": row["location"],
        "sources": sources,
    }


def _get_active_entry(db: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM entries
        WHERE user_id = ? AND end_time IS NULL AND deleted_at IS NULL
        ORDER BY work_date DESC, start_time DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def get_live_status(db: sqlite3.Connection, user: Dict[str, Any]) -> Dict[str, Any]:
    now_dt = user_now(user["timezone"])
    today_iso = now_dt.date().isoformat()
    now_min = now_dt.hour * 60 + now_dt.minute
    now_hm = f"{now_dt.hour:02d}:{now_dt.minute:02d}"

    active_row = _get_active_entry(db, user["id"])
    today_rows = db.execute(
        """
        SELECT * FROM entries
        WHERE user_id = ? AND work_date = ? AND deleted_at IS NULL
        ORDER BY start_time ASC, id ASC
        """,
        (user["id"], today_iso),
    ).fetchall()

    completed_today = [_row_to_entry(r) for r in today_rows if r["end_time"] is not None]
    today_completed_minutes = sum(e["minutes"] for e in completed_today)

    active_elapsed_minutes = 0
    intervals_for_breaks: List[Tuple[int, int]] = [
        (hm_to_minutes(e["start_time"]), hm_to_minutes(e["end_time"]))
        for e in completed_today
    ]

    active_entry_payload: Optional[Dict[str, Any]] = None
    if active_row is not None:
        active_entry_payload = {
            "id": int(active_row["id"]),
            "start": active_row["start_time"],
            "location": active_row["location"],
            "date": active_row["work_date"],
        }
        if active_row["work_date"] == today_iso:
            s_min = hm_to_minutes(active_row["start_time"])
            active_elapsed_minutes = max(0, now_min - s_min)
            intervals_for_breaks.append((s_min, max(s_min, now_min)))

    intervals_for_breaks.sort(key=lambda pair: (pair[0], pair[1]))
    break_actual_minutes = 0
    for idx in range(1, len(intervals_for_breaks)):
        prev_end = intervals_for_breaks[idx - 1][1]
        curr_start = intervals_for_breaks[idx][0]
        if curr_start > prev_end:
            break_actual_minutes += curr_start - prev_end

    gross_today = today_completed_minutes + active_elapsed_minutes
    break_required_minutes = 0
    if gross_today > (9 * 60 + 45):
        break_required_minutes = 45
    elif gross_today > (6 * 60 + 30):
        break_required_minutes = 30

    warnings: List[str] = []
    if active_row is not None and active_row["work_date"] < today_iso:
        warnings.append(
            f"Unclosed punch from {active_row['work_date']} (started {active_row['start_time']})"
        )

    if gross_today > (9 * 60 + 45) and break_actual_minutes < 45:
        warnings.append(f"Over 9h45 without required 45m break (missing {45 - break_actual_minutes}m)")
    elif (9 * 60 + 15) <= gross_today <= (9 * 60 + 45) and break_actual_minutes < 45:
        warnings.append("Approaching 9h45 without 45m break")
    elif gross_today > (6 * 60 + 30) and break_actual_minutes < 30:
        warnings.append(f"Over 6h30 without required 30m break (missing {30 - break_actual_minutes}m)")
    elif (6 * 60) <= gross_today <= (6 * 60 + 30) and break_actual_minutes < 30:
        warnings.append("Approaching 6h30 without 30m break")

    user_verb_date = (user.get("verbeamtung_date") or "").strip() or None
    is_beamter_today = (today_iso >= user_verb_date) if user_verb_date else (int(user["target_minutes"]) == S_MIN)
    if is_beamter_today:
        presence_today = gross_today + break_actual_minutes
        if presence_today > D_MAX_BEAMTER_BRUTTO:
            warnings.append(
                f"Exceeded 13h daily maximum incl. breaks (§ 4 AZV) by {presence_today - D_MAX_BEAMTER_BRUTTO}m"
            )
        elif 750 <= presence_today <= D_MAX_BEAMTER_BRUTTO:
            warnings.append("Approaching 13h daily maximum incl. breaks (§ 4 AZV)")
    else:
        if gross_today > D_MAX_TARIF:
            warnings.append(f"Exceeded 10h daily maximum by {gross_today - D_MAX_TARIF}m")
        elif 570 <= gross_today <= D_MAX_TARIF:
            warnings.append("Approaching 10h daily maximum")

    if active_row is not None and active_row["work_date"] == today_iso:
        today_d = now_dt.date()
        if today_d.weekday() >= 5:
            warnings.append("Weekend work is outside Rahmenarbeitszeit")
        else:
            r_end = rahmenzeit_end_min(today_d)
            if now_min < R_START_MIN or now_min > r_end:
                warnings.append(f"Outside Rahmenarbeitszeit 06:00–{minutes_to_hm(r_end)}")
            elif r_end - 15 <= now_min <= r_end:
                warnings.append(f"Approaching Rahmenarbeitszeit end ({minutes_to_hm(r_end)})")

    if active_entry_payload:
        last_location = active_entry_payload["location"]
    else:
        last_row = db.execute(
            """
            SELECT location FROM entries
            WHERE user_id = ? AND deleted_at IS NULL AND location IN ('remote', 'office', 'dg')
            ORDER BY work_date DESC, start_time DESC, id DESC
            LIMIT 1
            """,
            (user["id"],),
        ).fetchone()
        last_location = last_row["location"] if last_row else "remote"

    meta_row = db.execute(
        "SELECT day_code, note FROM day_meta WHERE user_id = ? AND work_date = ?",
        (user["id"], today_iso),
    ).fetchone()

    return {
        "clocked_in": active_entry_payload is not None,
        "active_entry": active_entry_payload,
        "today_date": today_iso,
        "now_hm": now_hm,
        "today_completed_minutes": today_completed_minutes,
        "active_elapsed_minutes": active_elapsed_minutes,
        "today_gross_minutes": gross_today,
        "break_required_minutes": break_required_minutes,
        "break_actual_minutes": break_actual_minutes,
        "warnings": warnings,
        "last_location": last_location,
        "today_entries": [_row_to_entry(r) for r in today_rows],
        "today_day_code": (meta_row["day_code"] or "") if meta_row else "",
        "today_note": (meta_row["note"] or "") if meta_row else "",
    }


def execute_punch(
    db: sqlite3.Connection,
    user: Dict[str, Any],
    action: str = "toggle",
    location: Optional[str] = None,
    client_ts: Optional[str] = None,
    event_id: Optional[str] = None,
    source: str = "web",
) -> Dict[str, Any]:
    clean_action = (action or "toggle").strip().lower()
    if clean_action not in ("in", "out", "toggle", "switch_location"):
        raise HTTPException(status_code=400, detail=f"Unsupported punch action '{action}'")

    clean_event_id = (event_id or "").strip() or None
    if clean_event_id:
        seen_ev = db.execute(
            "SELECT 1 FROM punch_events WHERE user_id = ? AND event_id = ?",
            (user["id"], clean_event_id),
        ).fetchone()
        seen_entry = db.execute(
            "SELECT 1 FROM entries WHERE user_id = ? AND client_event_id = ?",
            (user["id"], clean_event_id),
        ).fetchone()
        if seen_ev is not None or seen_entry is not None:
            return get_live_status(db, user)

    dt = parse_client_ts(client_ts, user["timezone"])
    work_date = dt.date().isoformat()
    hm = f"{dt.hour:02d}:{dt.minute:02d}"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    active = _get_active_entry(db, user["id"])
    clean_loc = (location or "").strip().lower()
    if clean_loc not in ("remote", "office", "dg"):
        clean_loc = active["location"] if active is not None else "remote"

    if clean_action == "toggle":
        effective = "out" if active is not None else "in"
    else:
        effective = clean_action

    if effective == "switch_location":
        if active is None:
            effective = "in"
        elif active["location"] != clean_loc:
            if active["work_date"] == work_date:
                end_hm = hm if hm >= active["start_time"] else active["start_time"]
                db.execute(
                    "UPDATE entries SET end_time = ?, updated_at = ? WHERE id = ?",
                    (end_hm, now_utc, active["id"]),
                )
            else:
                db.execute(
                    "UPDATE entries SET end_time = ?, updated_at = ? WHERE id = ?",
                    (active["start_time"], now_utc, active["id"]),
                )
            db.execute(
                """
                INSERT INTO entries (
                    user_id, work_date, start_time, end_time, location,
                    sources_json, client_event_id, deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?)
                """,
                (user["id"], work_date, hm, clean_loc, json.dumps([source]), clean_event_id, now_utc, now_utc),
            )
    elif effective == "in":
        if active is not None:
            if active["work_date"] == work_date and location and active["location"] != clean_loc:
                end_hm = hm if hm >= active["start_time"] else active["start_time"]
                db.execute(
                    "UPDATE entries SET end_time = ?, updated_at = ? WHERE id = ?",
                    (end_hm, now_utc, active["id"]),
                )
                db.execute(
                    """
                    INSERT INTO entries (
                        user_id, work_date, start_time, end_time, location,
                        sources_json, client_event_id, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?)
                    """,
                    (user["id"], work_date, hm, clean_loc, json.dumps([source]), clean_event_id, now_utc, now_utc),
                )
        else:
            db.execute(
                """
                INSERT INTO entries (
                    user_id, work_date, start_time, end_time, location,
                    sources_json, client_event_id, deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?)
                """,
                (user["id"], work_date, hm, clean_loc, json.dumps([source]), clean_event_id, now_utc, now_utc),
            )
    elif effective == "out":
        if active is not None:
            if active["work_date"] == work_date:
                end_hm = hm if hm >= active["start_time"] else active["start_time"]
            else:
                active_d = date.fromisoformat(active["work_date"])
                end_hm = minutes_to_hm(rahmenzeit_end_min(active_d)) if active_d.weekday() < 5 else "20:00"
                if end_hm < active["start_time"]:
                    end_hm = active["start_time"]
            db.execute(
                "UPDATE entries SET end_time = ?, updated_at = ? WHERE id = ?",
                (end_hm, now_utc, active["id"]),
            )

    if clean_event_id:
        db.execute(
            """
            INSERT OR IGNORE INTO punch_events (user_id, event_id, action, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user["id"], clean_event_id, clean_action, now_utc),
        )

    db.commit()
    return get_live_status(db, user)


def list_user_months(db: sqlite3.Connection, user_id: int, current_ym: str) -> List[str]:
    months = {current_ym}
    for row in db.execute(
        "SELECT DISTINCT substr(work_date, 1, 7) AS ym FROM entries WHERE user_id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchall():
        if row["ym"]:
            months.add(row["ym"])
    for row in db.execute(
        "SELECT DISTINCT substr(work_date, 1, 7) AS ym FROM day_meta WHERE user_id = ?",
        (user_id,),
    ).fetchall():
        if row["ym"]:
            months.add(row["ym"])
    return sorted(months, reverse=True)


def detect_soft_anomalies(
    year_month: str,
    all_entries: List[Dict[str, Any]],
    day_codes: Dict[str, str],
    has_atoss_import: bool,
    today_iso: str,
) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for e in all_entries:
        by_day.setdefault(e["work_date"], []).append(e)

    has_non_work_in_month = any(
        any(s in ("watch", "web", "app") for s in e["sources"])
        for e in all_entries
    )

    for e in all_entries:
        if e["end_time"] is None and e["work_date"] < today_iso:
            anomalies.append({
                "type": "unclosed_punch",
                "date": e["work_date"],
                "entry_id": e["id"],
                "message": f"Unclosed punch starting {e['start_time']} (no clock-out time)",
                "suggested_check": "Set clock-out time or check end of workday.",
            })
        if (e.get("location") or "").strip() in ("", "-", "?"):
            anomalies.append({
                "type": "unknown_location",
                "date": e["work_date"],
                "entry_id": e["id"],
                "message": f"Location missing ('{e.get('location') or ''}') for {e['start_time']}–{e['end_time'] or 'now'}",
                "suggested_check": "Choose remote or office.",
            })

    year, month = map(int, year_month.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    for dom in range(1, num_days + 1):
        d = date(year, month, dom)
        iso = d.isoformat()
        if iso > today_iso:
            continue
        day_all = by_day.get(iso, [])
        day_entries = [e for e in day_all if e["end_time"] is not None]
        code = (day_codes.get(iso) or "").upper()
        worked = sum(e["minutes"] for e in day_entries)
        if d.weekday() < 5 and 0 < worked < 240 and code not in VALID_DAY_CODES:
            anomalies.append({
                "type": "short_weekday_no_code",
                "date": iso,
                "message": f"Only {format_minutes(worked)} logged on weekday with no absence code",
                "suggested_check": "Check Google Calendar / Maps Timeline for missed punches or half-day ZA/K/U.",
            })

        if has_atoss_import and has_non_work_in_month and day_entries:
            work_only = [e for e in day_entries if "work" in e["sources"] and not any(s in ("watch", "web", "app") for s in e["sources"])]
            client_only = [e for e in day_entries if any(s in ("watch", "web", "app") for s in e["sources"]) and "work" not in e["sources"]]
            has_conflict = False
            for w in work_only:
                ws, we = hm_to_minutes(w["start_time"]), hm_to_minutes(w["end_time"])
                wloc = (w.get("location") or "").strip().lower()
                for c in client_only:
                    cs, ce = hm_to_minutes(c["start_time"]), hm_to_minutes(c["end_time"])
                    cloc = (c.get("location") or "").strip().lower()
                    if (ws < ce and cs < we) or (wloc and cloc and wloc == cloc):
                        has_conflict = True
                        break
                if has_conflict:
                    break
            if has_conflict:
                anomalies.append({
                    "type": "atoss_mismatch",
                    "date": iso,
                    "message": "ATOSS PDF intervals differ from Watch/Web punches on this day",
                    "suggested_check": "Review overlapping/differing intervals and resolve to preferred source.",
                })

    return sorted(anomalies, key=lambda x: (x["date"], x["type"]))


def _date_span_label(dates: List[str]) -> str:
    if not dates:
        return ""
    if len(dates) == 1:
        return dates[0]
    return f"{dates[0]} – {dates[-1]} ({len(dates)} days)"


def group_suspicious_days(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse suspicious days sharing a reason into one row with a date span."""
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        check = str(item.get("suggested_check") or "").strip()
        day = str(item.get("date") or "").strip()
        bucket = buckets.setdefault((reason, check), {"reason": reason, "suggested_check": check, "dates": []})
        if day:
            bucket["dates"].append(day)

    groups: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        dates = sorted(bucket["dates"])
        groups.append({
            "reason": bucket["reason"],
            "suggested_check": bucket["suggested_check"],
            "dates": dates,
            "count": len(dates),
            "first_date": dates[0] if dates else "",
            "date_label": _date_span_label(dates),
        })
    groups.sort(key=lambda g: (g["first_date"], g["reason"]))
    return groups


def get_month_data(db: sqlite3.Connection, user: Dict[str, Any], year_month: str) -> Dict[str, Any]:
    year_month = normalize_ym(year_month)
    year, month = map(int, year_month.split("-"))
    now_dt = user_now(user["timezone"])
    today_iso = now_dt.date().isoformat()
    current_ym = now_dt.strftime("%Y-%m")

    entry_rows = db.execute(
        """
        SELECT * FROM entries
        WHERE user_id = ? AND work_date LIKE ? AND deleted_at IS NULL
        ORDER BY work_date ASC, start_time ASC, id ASC
        """,
        (user["id"], f"{year_month}-%"),
    ).fetchall()
    all_entries = [_row_to_entry(r) for r in entry_rows]

    meta_rows = db.execute(
        "SELECT work_date, day_code, note FROM day_meta WHERE user_id = ? AND work_date LIKE ?",
        (user["id"], f"{year_month}-%"),
    ).fetchall()
    day_codes: Dict[str, str] = {}
    day_notes: Dict[str, str] = {}
    for r in meta_rows:
        if r["day_code"]:
            day_codes[r["work_date"]] = r["day_code"]
        if r["note"]:
            day_notes[r["work_date"]] = r["note"]

    completed_for_val = [
        {
            "id": str(e["id"]),
            "date": e["work_date"],
            "start": e["start_time"],
            "end": e["end_time"],
            "minutes": e["minutes"],
            "location": e["location"],
            "sources": e["sources"],
        }
        for e in all_entries
        if e["end_time"] is not None
    ]
    user_verb_date = (user.get("verbeamtung_date") or "").strip() or None
    val_res = run_validation(
        completed_for_val,
        extra_day_codes=day_codes,
        verbeamtung_date=user_verb_date,
        default_target=int(user["target_minutes"]),
    )
    hard_issues: List[Dict[str, Any]] = []
    for raw_it in val_res.get("issues", []):
        it = dict(raw_it)
        itype = it.get("type", "")
        it["label"] = ISSUE_LABELS.get(itype, itype.replace("_", " ").title())
        if itype == "insufficient_breaks":
            it["message"] = (
                f"Break below legal minimum: {it.get('actual', 0)}m taken, "
                f"{it.get('required', 30)}m required (missing {it.get('missing', 30)}m)"
            )
        elif itype == "over_daily_max":
            if it.get("max_minutes") == D_MAX_BEAMTER_BRUTTO:
                it["message"] = (
                    f"Total time incl. breaks is {format_minutes(it.get('presence_minutes', it.get('gross_minutes', 0)))}, "
                    f"exceeding 13:00 max (§ 4 AZV) by {it.get('excess', 0)}m"
                )
            else:
                it["message"] = (
                    f"Worked {format_minutes(it.get('gross_minutes', 0))}, "
                    f"exceeding 10:00 daily max by {it.get('excess', 0)}m"
                )
        elif itype.startswith("overlap_"):
            a = it.get("a") or {}
            b = it.get("b") or {}
            it["message"] = (
                f"Intervals {a.get('start', '')}–{a.get('end', '')} and "
                f"{b.get('start', '')}–{b.get('end', '')} overlap by {it.get('minutes', 0)}m"
            )
        hard_issues.append(it)

    has_atoss_import = db.execute(
        "SELECT 1 FROM atoss_imports WHERE user_id = ? AND year_month = ? LIMIT 1",
        (user["id"], year_month),
    ).fetchone() is not None

    soft_anomalies = detect_soft_anomalies(
        year_month=year_month,
        all_entries=all_entries,
        day_codes=day_codes,
        has_atoss_import=has_atoss_import,
        today_iso=today_iso,
    )
    for anom in soft_anomalies:
        atype = anom.get("type", "")
        anom["label"] = ISSUE_LABELS.get(atype, atype.replace("_", " ").title())

    last_dom = calendar.monthrange(year, month)[1]
    if year_month < current_ym:
        cutoff_iso = date(year, month, last_dom).isoformat()
    elif year_month == current_ym:
        cutoff_iso = today_iso
    else:
        cutoff_iso = None

    by_day_entries: Dict[str, List[Dict[str, Any]]] = {}
    for e in all_entries:
        by_day_entries.setdefault(e["work_date"], []).append(e)

    hard_by_day: Dict[str, List[Dict[str, Any]]] = {}
    for iss in hard_issues:
        if iss.get("date"):
            hard_by_day.setdefault(iss["date"], []).append(iss)

    soft_by_day: Dict[str, List[Dict[str, Any]]] = {}
    for anom in soft_anomalies:
        if anom.get("date"):
            soft_by_day.setdefault(anom["date"], []).append(anom)

    days_list: List[Dict[str, Any]] = []
    empty_workdays: List[str] = []
    workdays_so_far = 0
    expected_minutes = 0
    worked_minutes = 0
    yesterday_worked_minutes = 0
    yesterday_expected_minutes = 0
    now_min = now_dt.hour * 60 + now_dt.minute
    target_min = int(user["target_minutes"])

    for dom in range(1, last_dom + 1):
        d = date(year, month, dom)
        iso = d.isoformat()
        is_weekend = d.weekday() >= 5
        d_entries = by_day_entries.get(iso, [])
        code = (day_codes.get(iso) or "").upper()
        note = day_notes.get(iso, "")

        if is_weekend and not d_entries and code != "ZA":
            continue

        completed = [e for e in d_entries if e["end_time"] is not None]
        day_worked = sum(e["minutes"] for e in completed)
        if iso == today_iso:
            for e in d_entries:
                if e["end_time"] is None:
                    day_worked += max(0, now_min - hm_to_minutes(e["start_time"]))
        worked_minutes += day_worked
        if iso < today_iso:
            yesterday_worked_minutes += day_worked

        break_min = 0
        for idx in range(1, len(completed)):
            gap = hm_to_minutes(completed[idx]["start_time"]) - hm_to_minutes(completed[idx - 1]["end_time"])
            if gap > 0:
                break_min += gap

        day_base_target = day_target_for_user(iso, target_min, user_verb_date)
        is_counted_so_far = cutoff_iso is not None and iso <= cutoff_iso
        if not is_weekend and is_counted_so_far and code not in ZERO_SOLL_CODES:
            workdays_so_far += 1
            expected_minutes += day_base_target
            if iso < today_iso:
                yesterday_expected_minutes += day_base_target
        if not is_weekend and is_counted_so_far and not d_entries and code not in VALID_DAY_CODES:
            empty_workdays.append(iso)
            if iso < today_iso:
                empty_issue = {
                    "type": "empty_workday",
                    "label": ISSUE_LABELS["empty_workday"],
                    "date": iso,
                    "message": "No work entries or absence code logged on this workday",
                }
                hard_issues.append(empty_issue)
                hard_by_day.setdefault(iso, []).append(empty_issue)

        day_soll = 0 if (is_weekend and code != "ZA") or (code in ZERO_SOLL_CODES) else day_base_target
        day_hard = hard_by_day.get(iso, [])
        day_soft = soft_by_day.get(iso, [])

        quick_fixes: List[Dict[str, Any]] = []
        issue_types = {it.get("type") for it in day_hard}
        if "insufficient_breaks" in issue_types:
            quick_fixes.append({
                "fix_type": "insert_break",
                "label": "Insert missing break around midday",
            })
        for it in day_hard:
            if it.get("type") == "outside_rahmenzeit" and not is_weekend:
                quick_fixes.append({
                    "fix_type": "shift_rahmenzeit",
                    "entry_id": it.get("id"),
                    "label": f"Shift {it.get('start')}–{it.get('end')} into Rahmenzeit",
                })
        if any(t in issue_types for t in ("overlap_cross_sources", "overlap_within_source")):
            if any("work" in e["sources"] for e in completed):
                quick_fixes.append({
                    "fix_type": "resolve_overlap",
                    "mode": "prefer_work",
                    "label": "Prefer ATOSS times",
                })
            quick_fixes.append({
                "fix_type": "resolve_overlap",
                "mode": "merge",
                "label": "Merge overlapping intervals",
            })

        days_list.append({
            "date": iso,
            "date_fmt": f"{d.day:02d}.{d.month:02d}.{d.year}",
            "weekday_en": d.strftime("%a"),
            "weekday_de": GERMAN_WEEKDAYS.get(d.strftime("%a"), d.strftime("%a")),
            "week_num": d.isocalendar()[1],
            "is_weekend": is_weekend,
            "is_future": cutoff_iso is None or iso > cutoff_iso,
            "is_today": iso == today_iso,
            "day_code": code,
            "day_code_label": STATUS_MAP.get(code, code) if code else "",
            "note": note,
            "entries": d_entries,
            "worked_minutes": day_worked,
            "worked_fmt": format_minutes(day_worked),
            "break_minutes": break_min,
            "soll_minutes": day_soll,
            "delta_minutes": day_worked - day_soll,
            "delta_fmt": format_minutes(day_worked - day_soll, signed=True),
            "hard_issues": day_hard,
            "soft_anomalies": day_soft,
            "quick_fixes": quick_fixes,
        })

    balance_delta_minutes = worked_minutes - expected_minutes

    if user_verb_date and target_min == S_MIN:
        verb_ym = user_verb_date[:7]
        if year_month == verb_ym and user_verb_date[8:10] != "01":
            vd = date.fromisoformat(user_verb_date)
            prev_d = vd - timedelta(days=1)
            target_label = f"07:48 (until {prev_d.day:02d}.{prev_d.month:02d}.) · 08:12 (from {vd.day:02d}.{vd.month:02d}.)"
        elif year_month < verb_ym:
            target_label = "07:48"
        else:
            target_label = format_minutes(target_min)
    else:
        target_label = format_minutes(target_min)

    proposal_rows = db.execute(
        """
        SELECT * FROM proposals
        WHERE user_id = ? AND year_month = ? AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user["id"], year_month),
    ).fetchall()
    pending_proposals = []
    for p in proposal_rows:
        raw_changes = json.loads(p["changes_json"])
        raw_suspicious = json.loads(p["suspicious_days_json"])
        enriched_changes = []
        for ch in raw_changes:
            c_item = dict(ch)
            op = c_item.get("op", "")
            c_item["op_label"] = OP_LABELS.get(op, op.replace("_", " ").title())
            enriched_changes.append(c_item)
        pending_proposals.append({
            "id": int(p["id"]),
            "year_month": p["year_month"],
            "source": p["source"],
            "source_label": "External Agent" if p["source"] == "external_agent" else "AI Assistant",
            "status": p["status"],
            "summary": p["summary"],
            "suspicious_days": raw_suspicious,
            "suspicious_groups": group_suspicious_days(raw_suspicious),
            "changes": enriched_changes,
            "created_at": p["created_at"],
        })

    import_rows = db.execute(
        """
        SELECT id, filename, parsed_summary_json, imported_at
        FROM atoss_imports
        WHERE user_id = ? AND year_month = ?
        ORDER BY id DESC
        """,
        (user["id"], year_month),
    ).fetchall()
    atoss_imports = [
        {
            "id": int(r["id"]),
            "filename": r["filename"],
            "summary": json.loads(r["parsed_summary_json"]),
            "imported_at": r["imported_at"],
        }
        for r in import_rows
    ]

    month_name_de = f"{GERMAN_MONTH_NAMES.get(month, f'{month:02d}')} {year}"
    month_label = date(year, month, 1).strftime("%B %Y")
    overall_balance = compute_overall_balance(db, user, through_ym=year_month)
    balance_delta_yesterday_minutes = (
        yesterday_worked_minutes - yesterday_expected_minutes
        if year_month == current_ym
        else None
    )

    return {
        "year_month": year_month,
        "year": year,
        "month": month,
        "month_label": month_label,
        "month_name_de": month_name_de,
        "worked_minutes": worked_minutes,
        "worked_fmt": format_minutes(worked_minutes),
        "expected_minutes": expected_minutes,
        "expected_fmt": format_minutes(expected_minutes),
        "balance_delta_minutes": balance_delta_minutes,
        "balance_delta_fmt": format_minutes(balance_delta_minutes, signed=True),
        "balance_delta_yesterday_minutes": balance_delta_yesterday_minutes,
        "balance_delta_yesterday_fmt": (
            format_minutes(balance_delta_yesterday_minutes, signed=True)
            if balance_delta_yesterday_minutes is not None
            else None
        ),
        "overall_balance": overall_balance,
        "workdays_so_far": workdays_so_far,
        "target_minutes": target_min,
        "target_label": target_label,
        "hard_issues": hard_issues,
        "soft_anomalies": soft_anomalies,
        "empty_workdays": empty_workdays,
        "days": days_list,
        "entries": all_entries,
        "day_codes": day_codes,
        "day_notes": day_notes,
        "pending_proposals": pending_proposals,
        "atoss_imports": atoss_imports,
        "available_months": list_user_months(db, user["id"], current_ym),
    }


def add_entry(
    db: sqlite3.Connection,
    user_id: int,
    work_date: str,
    start_time: str,
    end_time: Optional[str],
    location: str = "remote",
    sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    d_iso = normalize_date(work_date)
    s_hm = normalize_hm(start_time)
    e_hm = normalize_hm(end_time) if end_time else None
    if e_hm is not None and hm_to_minutes(e_hm) < hm_to_minutes(s_hm):
        raise HTTPException(status_code=400, detail="End time must be after start time")
    loc = (location or "remote").strip() or "-"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cur = db.execute(
        """
        INSERT INTO entries (
            user_id, work_date, start_time, end_time, location,
            sources_json, client_event_id, deleted_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        (user_id, d_iso, s_hm, e_hm, loc, json.dumps(sources or ["manual"]), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_entry(row)


def update_entry(
    db: sqlite3.Connection,
    user_id: int,
    entry_id: int,
    start_time: str,
    end_time: Optional[str],
    location: str,
) -> Dict[str, Any]:
    row = db.execute(
        "SELECT * FROM entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    s_hm = normalize_hm(start_time)
    e_hm = normalize_hm(end_time) if (end_time is not None and str(end_time).strip() != "") else None
    if e_hm is not None and hm_to_minutes(e_hm) < hm_to_minutes(s_hm):
        raise HTTPException(status_code=400, detail="End time must be after start time")
    loc = (location or "-").strip() or "-"
    sources = json.loads(row["sources_json"] or "[]")
    if "manual" not in sources:
        sources.append("manual")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db.execute(
        """
        UPDATE entries
        SET start_time = ?, end_time = ?, location = ?, sources_json = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (s_hm, e_hm, loc, json.dumps(sources), now, entry_id, user_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _row_to_entry(updated)


def delete_entry(db: sqlite3.Connection, user_id: int, entry_id: int) -> str:
    row = db.execute(
        "SELECT work_date FROM entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cur = db.execute(
        "UPDATE entries SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (now, now, entry_id, user_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Entry not found")
    db.commit()
    return str(row["work_date"])


def set_day_meta(
    db: sqlite3.Connection,
    user_id: int,
    work_date: str,
    day_code: Optional[str],
    note: Optional[str],
) -> None:
    d_iso = normalize_date(work_date)
    code = (day_code or "").strip().upper() or None
    if code is not None and code not in VALID_DAY_CODES:
        raise HTTPException(status_code=400, detail=f"Invalid day code '{code}'")
    clean_note = (note or "").strip() or None
    if code is None and clean_note is None:
        db.execute("DELETE FROM day_meta WHERE user_id = ? AND work_date = ?", (user_id, d_iso))
    else:
        db.execute(
            """
            INSERT INTO day_meta (user_id, work_date, day_code, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, work_date) DO UPDATE SET
                day_code = excluded.day_code,
                note = excluded.note
            """,
            (user_id, d_iso, code, clean_note),
        )
    db.commit()


def apply_quick_fix(
    db: sqlite3.Connection,
    user: Dict[str, Any],
    year_month: str,
    fix_type: str,
    date_str: str,
    entry_id: Optional[int] = None,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    d_iso = normalize_date(date_str)
    d_obj = date.fromisoformat(d_iso)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = db.execute(
        """
        SELECT * FROM entries
        WHERE user_id = ? AND work_date = ? AND end_time IS NOT NULL AND deleted_at IS NULL
        ORDER BY start_time ASC, id ASC
        """,
        (user["id"], d_iso),
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail=f"No completed intervals on {d_iso}")

    if fix_type == "insert_break":
        entries = [_row_to_entry(r) for r in rows]
        gross = sum(e["minutes"] for e in entries)
        actual_break = 0
        for idx in range(1, len(entries)):
            gap = hm_to_minutes(entries[idx]["start_time"]) - hm_to_minutes(entries[idx - 1]["end_time"])
            if gap > 0:
                actual_break += gap
        required = 45 if gross > (9 * 60 + 45) else (30 if gross > (6 * 60 + 30) else 30)
        missing = max(15, required - actual_break) if actual_break < required else 30

        target = max(entries, key=lambda e: e["minutes"])
        s_min = hm_to_minutes(target["start_time"])
        e_min = hm_to_minutes(target["end_time"])
        if s_min <= 11 * 60 + 30 and e_min >= 13 * 60:
            split_min = 12 * 60
        else:
            split_min = s_min + (e_min - s_min) // 2

        new_e2 = e_min + missing
        r_end = rahmenzeit_end_min(d_obj) if d_obj.weekday() < 5 else 20 * 60
        shift_back = 0
        if new_e2 > r_end:
            shift_back = min(new_e2 - r_end, max(0, s_min - R_START_MIN))

        p1_start = s_min - shift_back
        p1_end = split_min - shift_back
        p2_start = split_min + missing - shift_back
        p2_end = new_e2 - shift_back

        sources = list(target["sources"])
        if "manual" not in sources:
            sources.append("manual")

        db.execute(
            "UPDATE entries SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (now, now, target["id"], user["id"]),
        )
        for a, b in ((p1_start, p1_end), (p2_start, p2_end)):
            db.execute(
                """
                INSERT INTO entries (
                    user_id, work_date, start_time, end_time, location,
                    sources_json, client_event_id, deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    user["id"],
                    d_iso,
                    minutes_to_hm(a),
                    minutes_to_hm(b),
                    target["location"],
                    json.dumps(sources),
                    now,
                    now,
                ),
            )
        db.commit()

    elif fix_type == "shift_rahmenzeit":
        if d_obj.weekday() >= 5:
            raise HTTPException(status_code=400, detail="Cannot shift weekend work into Rahmenzeit automatically")
        r_end = rahmenzeit_end_min(d_obj)
        targets = [r for r in rows if entry_id is None or int(r["id"]) == int(entry_id)]
        for r in targets:
            s_min = hm_to_minutes(r["start_time"])
            e_min = hm_to_minutes(r["end_time"])
            dur = min(e_min - s_min, r_end - R_START_MIN)
            if s_min < R_START_MIN:
                ns, ne = R_START_MIN, min(r_end, R_START_MIN + dur)
            elif e_min > r_end:
                ne = r_end
                ns = max(R_START_MIN, r_end - dur)
            else:
                continue
            sources = json.loads(r["sources_json"] or "[]")
            if "manual" not in sources:
                sources.append("manual")
            db.execute(
                """
                UPDATE entries SET start_time = ?, end_time = ?, sources_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (minutes_to_hm(ns), minutes_to_hm(ne), json.dumps(sources), now, r["id"], user["id"]),
            )
        db.commit()

    elif fix_type == "resolve_overlap":
        entries = [_row_to_entry(r) for r in rows]
        resolve_mode = (mode or "merge").strip().lower()
        if resolve_mode == "prefer_work":
            work_entries = [e for e in entries if "work" in e["sources"]]
            non_work = [e for e in entries if "work" not in e["sources"]]
            if work_entries and non_work:
                for nw in non_work:
                    nw_s, nw_e = hm_to_minutes(nw["start_time"]), hm_to_minutes(nw["end_time"])
                    for w in work_entries:
                        w_s, w_e = hm_to_minutes(w["start_time"]), hm_to_minutes(w["end_time"])
                        if min(nw_e, w_e) > max(nw_s, w_s):
                            db.execute(
                                "UPDATE entries SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                                (now, now, nw["id"], user["id"]),
                            )
                            break
                db.commit()
        else:
            # Merge overlapping intervals
            intervals = sorted(entries, key=lambda x: (hm_to_minutes(x["start_time"]), hm_to_minutes(x["end_time"])))
            groups: List[List[Dict[str, Any]]] = []
            for item in intervals:
                s_m = hm_to_minutes(item["start_time"])
                e_m = hm_to_minutes(item["end_time"])
                if not groups:
                    groups.append([item])
                    continue
                prev_end = max(hm_to_minutes(x["end_time"]) for x in groups[-1])
                if s_m < prev_end:
                    groups[-1].append(item)
                else:
                    groups.append([item])

            for grp in groups:
                if len(grp) <= 1:
                    continue
                ms = min(hm_to_minutes(x["start_time"]) for x in grp)
                me = max(hm_to_minutes(x["end_time"]) for x in grp)
                merged_sources: List[str] = []
                loc = "-"
                for x in grp:
                    for src in x["sources"]:
                        if src not in merged_sources:
                            merged_sources.append(src)
                    if "work" in x["sources"] and x["location"] not in ("", "-"):
                        loc = x["location"]
                    elif loc == "-" and x["location"] not in ("", "-"):
                        loc = x["location"]
                if "manual" not in merged_sources:
                    merged_sources.append("manual")
                for x in grp:
                    db.execute(
                        "UPDATE entries SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                        (now, now, x["id"], user["id"]),
                    )
                db.execute(
                    """
                    INSERT INTO entries (
                        user_id, work_date, start_time, end_time, location,
                        sources_json, client_event_id, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        user["id"],
                        d_iso,
                        minutes_to_hm(ms),
                        minutes_to_hm(me),
                        loc,
                        json.dumps(merged_sources),
                        now,
                        now,
                    ),
                )
            db.commit()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown quick-fix '{fix_type}'")

    return get_month_data(db, user, year_month)


def import_uploaded_file(
    db: sqlite3.Connection,
    user: Dict[str, Any],
    filename: str,
    content_bytes: bytes,
) -> Dict[str, Any]:
    suffix = Path(filename or "upload.pdf").suffix.lower() or ".pdf"
    if suffix not in (".pdf", ".txt"):
        raise HTTPException(status_code=400, detail="Only .pdf or .txt files are supported")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content_bytes)
        tmp_path = Path(tmp.name)

    try:
        parsed_entries: List[Dict[str, Any]] = []
        parsed_day_codes: Dict[str, str] = {}
        parsed_day_notes: Dict[str, str] = {}
        detected_kind = "atoss_pdf"

        if suffix == ".txt":
            detected_kind = "text_report"
            res = parse_report_entries(tmp_path)
            parsed_entries = res.get("entries", [])
            parsed_day_codes = res.get("day_codes", {})
            parsed_day_notes = res.get("day_notes", {})
        else:
            res = parse_atoss_pdf(tmp_path)
            parsed_entries = res.get("entries", [])
            parsed_day_codes = res.get("day_codes", {})
            if not parsed_entries and not parsed_day_codes:
                app_entries = parse_app_pdf(tmp_path)
                if app_entries:
                    detected_kind = "app_pdf"
                    parsed_entries = app_entries
    finally:
        tmp_path.unlink(missing_ok=True)

    months_seen = set()
    for e in parsed_entries:
        if e.get("date"):
            months_seen.add(e["date"][:7])
    for d in parsed_day_codes:
        months_seen.add(d[:7])

    if not months_seen:
        raise HTTPException(status_code=400, detail="No work intervals or day codes could be parsed from file")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    inserted_count = 0
    merged_count = 0
    codes_imported = 0

    existing_rows = db.execute(
        """
        SELECT * FROM entries
        WHERE user_id = ? AND deleted_at IS NULL
        """,
        (user["id"],),
    ).fetchall()
    existing_by_key: Dict[Tuple[str, str, str], sqlite3.Row] = {
        (r["work_date"], r["start_time"], r["end_time"]): r
        for r in existing_rows
        if r["end_time"] is not None
    }

    for item in parsed_entries:
        work_date = item["date"]
        s_hm = normalize_hm(item["start"])
        e_hm = normalize_hm(item["end"])
        loc = (item.get("location") or "-").strip() or "-"
        raw_src = item.get("source") or "work"
        in_sources = [s.strip() for s in str(raw_src).split(",") if s.strip()] or ["work"]

        key = (work_date, s_hm, e_hm)
        if key in existing_by_key:
            ex = existing_by_key[key]
            ex_sources = json.loads(ex["sources_json"] or "[]")
            for s in in_sources:
                if s not in ex_sources:
                    ex_sources.append(s)
            new_loc = ex["location"]
            if "work" in in_sources and loc not in ("", "-") and new_loc in ("", "-"):
                new_loc = loc
            elif loc == "remote" and new_loc in ("", "-"):
                new_loc = "remote"
            db.execute(
                "UPDATE entries SET sources_json = ?, location = ?, updated_at = ? WHERE id = ?",
                (json.dumps(ex_sources), new_loc, now, ex["id"]),
            )
            merged_count += 1
        else:
            cur = db.execute(
                """
                INSERT INTO entries (
                    user_id, work_date, start_time, end_time, location,
                    sources_json, client_event_id, deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (user["id"], work_date, s_hm, e_hm, loc, json.dumps(in_sources), now, now),
            )
            new_row = db.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
            existing_by_key[key] = new_row
            inserted_count += 1

    for d_iso, raw_code in parsed_day_codes.items():
        code = (raw_code or "").strip().upper()
        if code not in VALID_DAY_CODES:
            continue
        existing_meta = db.execute(
            "SELECT day_code, note FROM day_meta WHERE user_id = ? AND work_date = ?",
            (user["id"], d_iso),
        ).fetchone()
        note = parsed_day_notes.get(d_iso)
        if existing_meta is None:
            db.execute(
                "INSERT INTO day_meta (user_id, work_date, day_code, note) VALUES (?, ?, ?, ?)",
                (user["id"], d_iso, code, note),
            )
            codes_imported += 1
        elif not existing_meta["day_code"]:
            db.execute(
                "UPDATE day_meta SET day_code = ?, note = COALESCE(note, ?) WHERE user_id = ? AND work_date = ?",
                (code, note, user["id"], d_iso),
            )
            codes_imported += 1

    primary_ym = sorted(months_seen)[-1]
    summary_payload = {
        "kind": detected_kind,
        "months": sorted(months_seen),
        "inserted_entries": inserted_count,
        "merged_entries": merged_count,
        "day_codes_imported": codes_imported,
    }
    db.execute(
        """
        INSERT INTO atoss_imports (user_id, year_month, filename, parsed_summary_json, imported_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user["id"], primary_ym, filename or "upload.pdf", json.dumps(summary_payload), now),
    )
    db.commit()

    return {"year_month": primary_ym, **summary_payload}


def create_proposal(
    db: sqlite3.Connection,
    user_id: int,
    year_month: str,
    source: str,
    summary: str,
    suspicious_days: List[Dict[str, Any]],
    changes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    ym = normalize_ym(year_month)
    if source not in ("inbuilt_ai", "external_agent"):
        raise HTTPException(status_code=400, detail="Invalid proposal source")

    norm_changes: List[Dict[str, Any]] = []
    for idx, ch in enumerate(changes or [], start=1):
        if not isinstance(ch, dict):
            continue
        item = dict(ch)
        item["id"] = str(item.get("id") or f"chg_{idx}")
        item["accepted"] = bool(item.get("accepted", True))
        if isinstance(item.get("before"), dict):
            item["before"] = [item["before"]]
        if isinstance(item.get("after"), dict) and item.get("op") != "set_day_code":
            item["after"] = [item["after"]]
        norm_changes.append(item)

    norm_suspicious: List[Dict[str, Any]] = []
    for s in suspicious_days or []:
        if isinstance(s, dict):
            norm_suspicious.append(dict(s))
        elif isinstance(s, str) and s.strip():
            norm_suspicious.append({
                "date": s.strip(),
                "reason": "Flagged by AI",
                "suggested_check": "",
            })
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # A month has at most one proposal awaiting review: a newer run supersedes older pending ones.
    db.execute(
        """
        UPDATE proposals
        SET status = 'dismissed', resolved_at = ?
        WHERE user_id = ? AND year_month = ? AND status = 'pending'
        """,
        (now, user_id, ym),
    )
    cur = db.execute(
        """
        INSERT INTO proposals (
            user_id, year_month, source, status, summary,
            suspicious_days_json, changes_json, created_at, resolved_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, NULL)
        """,
        (
            user_id,
            ym,
            source,
            (summary or "AI Review Proposal").strip(),
            json.dumps(norm_suspicious),
            json.dumps(norm_changes),
            now,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM proposals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {
        "id": int(row["id"]),
        "year_month": row["year_month"],
        "source": row["source"],
        "status": row["status"],
        "summary": row["summary"],
        "suspicious_days": norm_suspicious,
        "changes": norm_changes,
        "created_at": row["created_at"],
    }


def apply_proposal(
    db: sqlite3.Connection,
    user_id: int,
    proposal_id: int,
    accepted_change_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    row = db.execute(
        "SELECT * FROM proposals WHERE id = ? AND user_id = ?",
        (proposal_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Proposal already {row['status']}")

    changes = json.loads(row["changes_json"])
    selected = set(accepted_change_ids) if accepted_change_ids is not None else None
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for ch in changes:
        cid = str(ch.get("id"))
        if selected is not None and cid not in selected:
            ch["accepted"] = False
            continue
        ch["accepted"] = True

        op = (ch.get("op") or "").strip()
        d_iso = normalize_date(str(ch.get("date") or ""))

        if op == "set_day_code":
            code = ch.get("day_code")
            if code is None and isinstance(ch.get("after"), dict):
                code = ch["after"].get("day_code")
            note = ch.get("note")
            if note is None and isinstance(ch.get("after"), dict):
                note = ch["after"].get("note")
            set_day_meta(db, user_id, d_iso, code, note)
            continue

        before_items = ch.get("before") or []
        after_items = ch.get("after") or []

        if op == "replace_day":
            db.execute(
                "UPDATE entries SET deleted_at = ?, updated_at = ? WHERE user_id = ? AND work_date = ? AND deleted_at IS NULL",
                (now, now, user_id, d_iso),
            )
        elif op in ("split_break", "replace_intervals", "update_interval", "delete_interval"):
            for b in before_items:
                b_start = normalize_hm(b["start"])
                b_end = normalize_hm(b["end"])
                db.execute(
                    """
                    UPDATE entries SET deleted_at = ?, updated_at = ?
                    WHERE user_id = ? AND work_date = ? AND start_time = ? AND end_time = ? AND deleted_at IS NULL
                    """,
                    (now, now, user_id, d_iso, b_start, b_end),
                )

        if op in ("split_break", "replace_intervals", "update_interval", "add_interval", "replace_day"):
            for a in after_items:
                a_start = normalize_hm(a["start"])
                a_end = normalize_hm(a["end"])
                if hm_to_minutes(a_end) < hm_to_minutes(a_start):
                    continue
                loc = (a.get("location") or "remote").strip() or "-"
                srcs = list(a.get("sources") or ["ai"])
                if "ai" not in srcs:
                    srcs.append("ai")
                db.execute(
                    """
                    INSERT INTO entries (
                        user_id, work_date, start_time, end_time, location,
                        sources_json, client_event_id, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (user_id, d_iso, a_start, a_end, loc, json.dumps(srcs), now, now),
                )

    db.execute(
        """
        UPDATE proposals
        SET status = 'applied', changes_json = ?, resolved_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (json.dumps(changes), now, proposal_id, user_id),
    )
    db.commit()
    return {"id": proposal_id, "year_month": row["year_month"], "status": "applied"}


def dismiss_proposal(db: sqlite3.Connection, user_id: int, proposal_id: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cur = db.execute(
        """
        UPDATE proposals
        SET status = 'dismissed', resolved_at = ?
        WHERE id = ? AND user_id = ? AND status = 'pending'
        """,
        (now, proposal_id, user_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Pending proposal not found")
    db.commit()
    return {"id": proposal_id, "status": "dismissed"}


def _build_heuristic_ai_proposal(
    month_data: Dict[str, Any],
    instruction: str = "",
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    changes: List[Dict[str, Any]] = []
    suspicious_days: List[Dict[str, Any]] = []
    ym = month_data["year_month"]
    year, month = map(int, ym.split("-"))

    # Parse natural-language absence instructions such as "Nov 14 was sick", "2026-09-22 K", "22.09. Urlaub"
    low_instr = (instruction or "").lower()
    code_match = None
    for kw, code in (
        ("sick", "K"), ("krank", "K"),
        ("urlaub", "U"), ("vacation", "U"), ("holiday", "F"), ("feiertag", "F"),
        ("zeitausgleich", "ZA"), ("comp", "ZA"), ("za", "ZA"),
        ("dienstreise", "D"), ("trip", "D"),
        ("sonderurlaub", "S"),
    ):
        if re.search(rf"\b{kw}\b", low_instr):
            code_match = code
            break

    if code_match:
        found_dates: List[str] = []
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", instruction):
            found_dates.append(m.group(1))
        for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(?:(\d{2,4}))?\b", instruction):
            dom, mon = int(m.group(1)), int(m.group(2))
            yr = int(m.group(3)) if m.group(3) else year
            if yr < 100:
                yr += 2000
            try:
                found_dates.append(date(yr, mon, dom).isoformat())
            except ValueError:
                pass
        for m in re.finditer(r"\b(?:jan|feb|mar|mär|apr|may|mai|jun|jul|aug|sep|sept|oct|okt|nov|dec|dez)[a-z]*\s+(\d{1,2})\b", low_instr):
            dom = int(m.group(1))
            try:
                found_dates.append(date(year, month, dom).isoformat())
            except ValueError:
                pass
        for d_iso in dict.fromkeys(found_dates):
            if d_iso.startswith(ym):
                changes.append({
                    "op": "set_day_code",
                    "date": d_iso,
                    "day_code": code_match,
                    "note": f"From instruction: {instruction.strip()}",
                    "before": [],
                    "after": [{"day_code": code_match}],
                    "reason": f"Mark {d_iso} as {STATUS_MAP.get(code_match, code_match)} per instruction.",
                })

    for day in month_data["days"]:
        d_iso = day["date"]
        d_obj = date.fromisoformat(d_iso)
        completed = [e for e in day["entries"] if e["end_time"] is not None]
        issue_types = {it.get("type") for it in day["hard_issues"]}

        if "insufficient_breaks" in issue_types and completed:
            iss = next(it for it in day["hard_issues"] if it.get("type") == "insufficient_breaks")
            missing = int(iss.get("missing") or 30)
            target = max(completed, key=lambda e: e["minutes"])
            s_min = hm_to_minutes(target["start_time"])
            e_min = hm_to_minutes(target["end_time"])
            split_min = 12 * 60 if (s_min <= 11 * 60 + 30 and e_min >= 13 * 60) else s_min + (e_min - s_min) // 2
            new_e2 = e_min + missing
            r_end = rahmenzeit_end_min(d_obj) if not day["is_weekend"] else 20 * 60
            shift_back = min(new_e2 - r_end, max(0, s_min - R_START_MIN)) if new_e2 > r_end else 0
            changes.append({
                "op": "split_break",
                "date": d_iso,
                "before": [{"start": target["start_time"], "end": target["end_time"], "location": target["location"]}],
                "after": [
                    {"start": minutes_to_hm(s_min - shift_back), "end": minutes_to_hm(split_min - shift_back), "location": target["location"]},
                    {"start": minutes_to_hm(split_min + missing - shift_back), "end": minutes_to_hm(new_e2 - shift_back), "location": target["location"]},
                ],
                "reason": f"Insert {missing}m break for >6h30 shift while keeping net worked duration.",
            })

        for it in day["hard_issues"]:
            if it.get("type") == "outside_rahmenzeit" and not day["is_weekend"]:
                r_end = rahmenzeit_end_min(d_obj)
                s_min = hm_to_minutes(it["start"])
                e_min = hm_to_minutes(it["end"])
                dur = min(e_min - s_min, r_end - R_START_MIN)
                if s_min < R_START_MIN:
                    ns, ne = R_START_MIN, min(r_end, R_START_MIN + dur)
                else:
                    ne = r_end
                    ns = max(R_START_MIN, r_end - dur)
                loc = next((e["location"] for e in completed if e["start_time"] == it["start"] and e["end_time"] == it["end"]), "remote")
                changes.append({
                    "op": "replace_intervals",
                    "date": d_iso,
                    "before": [{"start": it["start"], "end": it["end"], "location": loc}],
                    "after": [{"start": minutes_to_hm(ns), "end": minutes_to_hm(ne), "location": loc}],
                    "reason": f"Shift interval into Rahmenarbeitszeit 06:00–{minutes_to_hm(r_end)} preserving duration.",
                })

        if any(t in issue_types for t in ("overlap_cross_sources", "overlap_within_source")) and len(completed) >= 2:
            ms = min(hm_to_minutes(e["start_time"]) for e in completed)
            me = max(hm_to_minutes(e["end_time"]) for e in completed)
            pref_loc = next((e["location"] for e in completed if "work" in e["sources"] and e["location"] not in ("", "-")), completed[0]["location"])
            changes.append({
                "op": "replace_intervals",
                "date": d_iso,
                "before": [{"start": e["start_time"], "end": e["end_time"], "location": e["location"]} for e in completed],
                "after": [{"start": minutes_to_hm(ms), "end": minutes_to_hm(me), "location": pref_loc}],
                "reason": "Merge overlapping intervals on this day.",
            })

    seen_suspicious: set[str] = set()
    for anom in month_data["soft_anomalies"]:
        d_iso = anom["date"]
        if d_iso not in seen_suspicious:
            suspicious_days.append({
                "date": d_iso,
                "reason": anom["message"],
                "suggested_check": anom.get("suggested_check") or "Check Google Calendar / Maps Timeline.",
            })
            seen_suspicious.add(d_iso)

    for empty_iso in month_data["empty_workdays"]:
        if empty_iso not in seen_suspicious and not any(c.get("date") == empty_iso for c in changes):
            suspicious_days.append({
                "date": empty_iso,
                "reason": "Empty weekday with no punches and no absence code.",
                "suggested_check": "Check Google Calendar / Maps Timeline or mark as U/K/ZA/F.",
            })
            seen_suspicious.add(empty_iso)

    summary = (
        f"Prepared {len(changes)} proposed edit(s) and flagged {len(suspicious_days)} day(s) for review."
        if (changes or suspicious_days)
        else "No hard issues, missing breaks, or suspicious days detected for this month."
    )
    return summary, suspicious_days, changes


LLM_PROVIDERS: Dict[str, Dict[str, str]] = {
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "openai": {"label": "OpenAI / Compatible", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"label": "Anthropic Claude", "base_url": "https://api.anthropic.com/v1"},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1"},
}

REASONING_LEVELS: Tuple[str, ...] = ("off", "low", "medium", "high")
REASONING_BUDGETS: Dict[str, int] = {"low": 1024, "medium": 2048, "high": 4096}


def normalize_reasoning(value: Any) -> str:
    level = str(value or "").strip().lower()
    return level if level in REASONING_LEVELS else "off"


def reasoning_request_params(provider: str, level: str) -> Dict[str, Any]:
    """Translate an effort level into the provider's thinking knob."""
    clean = normalize_reasoning(level)
    if clean == "off":
        return {}
    budget = REASONING_BUDGETS[clean]
    if provider in ("openai", "mistral"):
        return {"reasoning_effort": clean}
    if provider == "anthropic":
        return {"thinking": {"type": "enabled", "budget_tokens": budget}, "max_tokens": budget + 2048}
    return {"_gemini_thinking_budget": budget}


def should_retry_without_reasoning(status_code: int, body: str) -> bool:
    """Providers reject the thinking knob for models that cannot reason."""
    if status_code not in (400, 422):
        return False
    lowered = (body or "").lower()
    return "reasoning" in lowered or "thinking" in lowered


def resolve_llm_config(user: Dict[str, Any]) -> Dict[str, Any]:
    provider = (user.get("llm_provider") or os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider not in LLM_PROVIDERS:
        provider = "gemini"
    api_key = (user.get("llm_api_key") or os.environ.get("LLM_API_KEY") or "").strip()
    model = (user.get("llm_model") or os.environ.get("LLM_MODEL") or "").strip()
    env_base_url = (user.get("llm_base_url") or os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
    # Ollama, LiteLLM, OpenRouter and friends are reached through the openai provider;
    # everything else uses its own fixed endpoint.
    if provider == "openai":
        base_url = env_base_url or LLM_PROVIDERS["openai"]["base_url"]
    else:
        base_url = LLM_PROVIDERS[provider]["base_url"]
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "reasoning": normalize_reasoning(user.get("llm_reasoning") or os.environ.get("LLM_REASONING")),
        "has_key": bool(api_key),
        "has_model": bool(model),
    }


def _openai_chat_url(base_url: str) -> str:
    clean = base_url.strip().rstrip("/")
    if clean.endswith("/chat/completions"):
        return clean
    return f"{clean}/chat/completions"


def _coerce_llm_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if (
                    item.get("type") in ("thinking", "reasoning", "thought")
                    or item.get("thought") is True
                    or "thinking" in item
                ):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                else:
                    parts.append(json.dumps(item))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return json.dumps(content)
    return "" if content is None else str(content)


def _parse_llm_json_dict(raw_text: Any, provider: str, model: str) -> Dict[str, Any]:
    cleaned = _coerce_llm_text(raw_text).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    parsed: Any
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                parsed = json.loads(cleaned[brace_start : brace_end + 1])
            except json.JSONDecodeError as inner_exc:
                raise AIConnectionError(
                    f"AI returned invalid JSON ({provider} · {model}): {inner_exc}"
                ) from inner_exc
        else:
            raise AIConnectionError(
                f"AI returned invalid JSON ({provider} · {model}): {exc}"
            ) from exc

    if isinstance(parsed, list):
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        elif all(isinstance(item, dict) and "op" in item for item in parsed):
            parsed = {"summary": "AI review proposal", "changes": parsed}
        else:
            raise AIConnectionError(
                f"AI returned unexpected JSON list ({provider} · {model})"
            )
    if not isinstance(parsed, dict):
        raise AIConnectionError(
            f"AI returned unexpected JSON type ({provider} · {model}): {type(parsed).__name__}"
        )
    return parsed


LLM_MODEL_BLOCKLIST = ("embed", "moderation", "whisper", "tts", "dall-e", "audio", "realtime", "transcribe")


def extract_model_ids(provider: str, payload: Any) -> List[str]:
    """Pull usable chat model ids out of a provider's model list payload."""
    ids: List[str] = []
    if provider == "gemini":
        for item in (payload or {}).get("models", []) if isinstance(payload, dict) else []:
            methods = item.get("supportedGenerationMethods") or []
            name = str(item.get("name") or "").replace("models/", "")
            if not name:
                continue
            if methods and "generateContent" not in methods:
                continue
            ids.append(name)
    else:
        for item in (payload or {}).get("data", []) if isinstance(payload, dict) else []:
            model_id = str(item.get("id") or "")
            if not model_id:
                continue
            capabilities = item.get("capabilities") or {}
            if capabilities and capabilities.get("completion_chat") is False:
                continue
            if any(token in model_id.lower() for token in LLM_MODEL_BLOCKLIST):
                continue
            ids.append(model_id)
    seen: List[str] = []
    for model_id in ids:
        if model_id not in seen:
            seen.append(model_id)
    return sorted(seen)


async def discover_models(cfg: Dict[str, Any], timeout_s: float = 20.0) -> List[str]:
    provider = cfg["provider"]
    api_key = cfg["api_key"]
    if not api_key:
        raise AIConnectionError("No API key saved yet — enter one to list the available models.")

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            if provider in ("openai", "mistral"):
                resp = await client.get(
                    f"{cfg['base_url'].rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "anthropic":
                resp = await client.get(
                    f"{cfg['base_url']}/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            else:
                resp = await client.get(
                    f"{cfg['base_url']}/models",
                    params={"key": api_key},
                )
            resp.raise_for_status()
            return extract_model_ids(provider, resp.json())
    except httpx.HTTPStatusError as exc:
        body_snippet = (exc.response.text or "")[:160].replace("\n", " ").strip()
        raise AIConnectionError(
            f"Could not list models ({provider}): HTTP {exc.response.status_code} {body_snippet}"
        ) from exc
    except httpx.RequestError as exc:
        raise AIConnectionError(f"Could not list models ({provider}): {exc}") from exc
    except Exception as exc:
        raise AIConnectionError(f"Could not list models ({provider}): {exc}") from exc


def _strip_reasoning(body: Dict[str, Any], provider: str) -> Dict[str, Any]:
    cleaned = dict(body)
    if provider in ("openai", "mistral"):
        cleaned.pop("reasoning_effort", None)
    elif provider == "anthropic":
        cleaned.pop("thinking", None)
        cleaned["max_tokens"] = 2048
    else:
        generation = dict(cleaned.get("generationConfig") or {})
        generation.pop("thinkingConfig", None)
        cleaned["generationConfig"] = generation
    return cleaned


def _llm_request_body(
    provider: str,
    cfg: Dict[str, Any],
    system_prompt: str,
    user_content: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the provider payload plus the reasoning metadata for this call."""
    reasoning = normalize_reasoning(cfg.get("reasoning"))
    thinking = reasoning_request_params(provider, reasoning)
    meta: Dict[str, Any] = {"reasoning": reasoning, "reasoning_tokens": 0, "reasoning_note": ""}

    if provider in ("openai", "mistral"):
        body: Dict[str, Any] = {
            "model": cfg["model"],
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        body.update(thinking)
    elif provider == "anthropic":
        body = {
            "model": cfg["model"],
            "max_tokens": thinking.get("max_tokens", 2048),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        if "thinking" in thinking:
            body["thinking"] = thinking["thinking"]
    else:
        generation: Dict[str, Any] = {"responseMimeType": "application/json"}
        if "_gemini_thinking_budget" in thinking:
            generation["thinkingConfig"] = {"thinkingBudget": thinking["_gemini_thinking_budget"]}
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": generation,
        }
    return body, meta


async def _post_llm(
    client: httpx.AsyncClient,
    provider: str,
    cfg: Dict[str, Any],
    body: Dict[str, Any],
) -> httpx.Response:
    api_key = cfg["api_key"]
    if provider in ("openai", "mistral"):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return await client.post(_openai_chat_url(cfg["base_url"]), headers=headers, json=body)
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        return await client.post(f"{cfg['base_url']}/messages", headers=headers, json=body)
    return await client.post(
        f"{cfg['base_url']}/models/{cfg['model']}:generateContent",
        params={"key": api_key},
        json=body,
    )


async def _call_llm_json(
    cfg: Dict[str, Any],
    system_prompt: str,
    user_content: str,
    timeout_s: float = 45.0,
) -> Tuple[str, Dict[str, Any]]:
    """Call the provider and return the raw text plus reasoning metadata."""
    provider = cfg["provider"]
    model = cfg["model"]
    body, meta = _llm_request_body(provider, cfg, system_prompt, user_content)
    has_response_format = "response_format" in body

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await _post_llm(client, provider, cfg, body)

            # Older/other OpenAI-compatible servers do not know response_format.
            if resp.status_code == 400 and has_response_format and "response_format" in resp.text:
                body.pop("response_format", None)
                resp = await _post_llm(client, provider, cfg, body)

            # Models without reasoning support reject the thinking knob outright.
            if should_retry_without_reasoning(resp.status_code, resp.text) and (
                meta["reasoning"] != "off"
            ):
                meta["reasoning_note"] = (
                    f"{model} does not accept reasoning_effort={meta['reasoning']} — retried without thinking"
                )
                body = _strip_reasoning(body, provider)
                resp = await _post_llm(client, provider, cfg, body)

            resp.raise_for_status()
            payload = resp.json()

        usage = payload.get("usage") or payload.get("usageMetadata") or {}
        details = usage.get("completion_tokens_details") or {}
        meta["reasoning_tokens"] = int(
            details.get("reasoning_tokens") or usage.get("thoughtsTokenCount") or 0
        )

        if provider in ("openai", "mistral"):
            text = _coerce_llm_text(payload["choices"][0]["message"].get("content"))
        elif provider == "anthropic":
            text = _coerce_llm_text(payload.get("content"))
        else:
            text = _coerce_llm_text(payload["candidates"][0]["content"].get("parts"))
        return text, meta
    except httpx.HTTPStatusError as exc:
        body_snippet = (exc.response.text or "")[:200].replace("\n", " ").strip()
        raise AIConnectionError(
            f"AI connection failed ({provider} · {model}): HTTP {exc.response.status_code} {body_snippet}"
        ) from exc
    except httpx.RequestError as exc:
        raise AIConnectionError(
            f"AI connection failed ({provider} · {model}): {exc}"
        ) from exc
    except AIConnectionError:
        raise
    except Exception as exc:
        raise AIConnectionError(
            f"AI response error ({provider} · {model}): {exc}"
        ) from exc


MISSING_MODEL_MESSAGE = (
    "No AI model selected. Open Settings → AI Assist Configuration, pick a provider, "
    "then use 'Discover models' to choose one."
)


async def test_llm_connection(user: Dict[str, Any]) -> Tuple[bool, str]:
    cfg = resolve_llm_config(user)
    if not cfg["has_key"] and not (cfg["provider"] == "openai" and user.get("llm_base_url")):
        return False, "No API key configured. Enter an API key (or a local OpenAI-compatible Base URL) to test."
    if not cfg["has_model"]:
        return False, MISSING_MODEL_MESSAGE
    try:
        raw, meta = await _call_llm_json(
            cfg,
            system_prompt='Return ONLY valid JSON: {"ok": true}',
            user_content='Reply with {"ok": true}',
            timeout_s=15.0,
        )
        if not raw.strip():
            return False, f"AI connection failed ({cfg['provider']} · {cfg['model']}): Empty response"
        detail = f"Connected successfully to {cfg['provider']} ({cfg['model']})"
        if meta["reasoning_tokens"]:
            detail += f" · thinking {meta['reasoning_tokens']} tokens"
        if meta["reasoning_note"]:
            detail += f" · {meta['reasoning_note']}"
        return True, detail
    except AIConnectionError as exc:
        return False, str(exc)


def _change_key(change: Dict[str, Any]) -> Tuple[str, str]:
    return (str(change.get("date") or "").strip(), str(change.get("op") or "").strip())


def merge_proposal_changes(
    rule_changes: List[Dict[str, Any]],
    ai_changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One change per date+op, rule-scan fixes first, model suggestions on top.

    The deterministic scan knows every hard rule violation, so it decides the
    baseline; the model may only add corrections the rules do not cover.
    """
    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for source, changes in (("rule_scan", rule_changes), ("ai", ai_changes)):
        for change in changes or []:
            if not isinstance(change, dict):
                continue
            item = dict(change)
            item["source"] = source
            key = _change_key(item)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def merge_suspicious_days(
    rule_days: List[Dict[str, Any]],
    ai_days: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Deduplicate flagged days by date, keeping the rule-scan wording first."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for source, days in (("rule_scan", rule_days), ("ai", ai_days)):
        for day in days or []:
            if isinstance(day, str):
                day = {"date": day, "reason": "Flagged for review", "suggested_check": ""}
            if not isinstance(day, dict):
                continue
            d_iso = str(day.get("date") or "").strip()
            if not d_iso or d_iso in seen:
                continue
            item = dict(day)
            item["source"] = source
            seen.add(d_iso)
            merged.append(item)
    return merged


async def generate_inbuilt_ai_proposal(
    db: sqlite3.Connection,
    user: Dict[str, Any],
    year_month: str,
    instruction: str = "",
    mode: str = "ai",
) -> Dict[str, Any]:
    month_data = get_month_data(db, user, year_month)
    clean_mode = (mode or "ai").strip().lower()

    if clean_mode == "heuristic":
        summary, suspicious_days, changes = _build_heuristic_ai_proposal(month_data, instruction)
        return create_proposal(
            db,
            user_id=user["id"],
            year_month=year_month,
            source="inbuilt_ai",
            summary=f"[Offline Rule Scan] {summary}",
            suspicious_days=suspicious_days,
            changes=changes,
        )

    cfg = resolve_llm_config(user)
    if not cfg["has_key"] and not (cfg["provider"] == "openai" and user.get("llm_base_url")):
        raise AIConnectionError(
            "No AI API key configured. Please add your API key in Settings → AI Assist Configuration "
            "(or click 'Offline Rule Scan' to run deterministic checks without AI)."
        )
    if not cfg["has_model"]:
        raise AIConnectionError(MISSING_MODEL_MESSAGE)

    # Rule scan first: it enumerates every hard violation deterministically, so a
    # single review covers all of them and the model can only add what rules miss.
    _, rule_suspicious, rule_changes = _build_heuristic_ai_proposal(month_data, instruction)

    compact_days = [
        {
            "date": d["date"],
            "weekday": d["weekday_en"],
            "day_code": d["day_code"],
            "note": d["note"],
            "worked": d["worked_fmt"],
            "entries": [
                {"start": e["start_time"], "end": e["end_time"], "location": e["location"], "sources": e["sources"]}
                for e in d["entries"]
                if e["end_time"] is not None
            ],
            "hard_issues": d["hard_issues"],
            "soft_anomalies": d["soft_anomalies"],
        }
        for d in month_data["days"]
        if d["entries"] or d["day_code"] or d["hard_issues"] or d["soft_anomalies"] or (d["date"] in month_data["empty_workdays"])
    ]
    prompt_payload = {
        "year_month": year_month,
        "instruction": instruction or "Review this month, propose fixes for hard rule violations (6h30->30m break, 9h45->45m break, 06:00-20:00 Fri 18:00), and list suspicious days.",
        "rule_scan_changes": [
            {"date": ch.get("date"), "op": ch.get("op"), "reason": ch.get("reason")}
            for ch in rule_changes
        ],
        "empty_workdays": month_data["empty_workdays"],
        "days": compact_days,
        "required_output_schema": {
            "summary": "string",
            "suspicious_days": [{"date": "YYYY-MM-DD", "reason": "string", "suggested_check": "string"}],
            "changes": [
                {
                    "op": "split_break | replace_intervals | add_interval | delete_interval | set_day_code",
                    "date": "YYYY-MM-DD",
                    "before": [{"start": "HH:MM", "end": "HH:MM", "location": "remote|office"}],
                    "after": [{"start": "HH:MM", "end": "HH:MM", "location": "remote|office"}],
                    "day_code": "optional K|S|U|F|ZA|D",
                    "reason": "string",
                }
            ],
        },
    }
    system_prompt = (
        "You are an ATOSS time-tracking review assistant. Return ONLY valid JSON matching required_output_schema. "
        "The rule scan already lists a fix for every hard violation in rule_scan_changes; do not repeat those "
        "date+op pairs. Add corrections for problems the rules cannot see, and flag days that need real data. "
        "Never invent worked hours without explanation; preserve worked duration when splitting breaks."
    )

    raw_text, reasoning_meta = await _call_llm_json(cfg, system_prompt, json.dumps(prompt_payload), timeout_s=90.0)
    parsed = _parse_llm_json_dict(raw_text, cfg["provider"], cfg["model"])

    changes = merge_proposal_changes(rule_changes, list(parsed.get("changes") or []))
    suspicious_days = merge_suspicious_days(rule_suspicious, list(parsed.get("suspicious_days") or []))
    rule_count = sum(1 for ch in changes if ch.get("source") == "rule_scan")
    ai_count = len(changes) - rule_count
    ai_summary = str(parsed.get("summary") or "AI review proposal").strip()
    coverage = (
        f"{len(changes)} edit(s) · {rule_count} from rule scan · {ai_count} from the assistant "
        f"· {len(suspicious_days)} day(s) flagged"
    )
    summary = f"{ai_summary} — {coverage}" if changes or suspicious_days else ai_summary

    proposal = create_proposal(
        db,
        user_id=user["id"],
        year_month=year_month,
        source="inbuilt_ai",
        summary=summary,
        suspicious_days=suspicious_days,
        changes=changes,
    )
    proposal["reasoning"] = reasoning_meta["reasoning"]
    proposal["reasoning_tokens"] = reasoning_meta["reasoning_tokens"]
    proposal["reasoning_note"] = reasoning_meta["reasoning_note"]
    return proposal


def export_months_pdf(db: sqlite3.Connection, user: Dict[str, Any], year_months: List[str], out_dir: Path) -> Path:
    if not year_months:
        year_months = [user_now(user["timezone"]).strftime("%Y-%m")]
    months_data: List[Dict[str, Any]] = []
    norm_months: List[str] = []
    for ym in year_months:
        clean_ym = normalize_ym(ym)
        norm_months.append(clean_ym)
        year, month = map(int, clean_ym.split("-"))
        entry_rows = db.execute(
            """
            SELECT work_date, start_time, end_time, location
            FROM entries
            WHERE user_id = ? AND work_date LIKE ? AND end_time IS NOT NULL AND deleted_at IS NULL
            ORDER BY work_date ASC, start_time ASC, id ASC
            """,
            (user["id"], f"{clean_ym}-%"),
        ).fetchall()
        entries = [
            {
                "date": r["work_date"],
                "start": r["start_time"],
                "end": r["end_time"],
                "location": r["location"],
            }
            for r in entry_rows
        ]
        meta_rows = db.execute(
            "SELECT work_date, day_code FROM day_meta WHERE user_id = ? AND work_date LIKE ? AND day_code IS NOT NULL",
            (user["id"], f"{clean_ym}-%"),
        ).fetchall()
        day_codes = {r["work_date"]: r["day_code"] for r in meta_rows if r["day_code"]}
        months_data.append(build_month_pdf_data(year, month, entries, day_codes=day_codes))

    out_pdf_path = out_dir / f"atoss_{'_'.join(norm_months)}.pdf"
    return compile_typst_pdf(months_data, out_pdf_path)
