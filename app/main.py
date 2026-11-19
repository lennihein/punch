from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.auth import (
    _row_to_user,
    create_api_token,
    create_pairing_code,
    exchange_pairing_code,
    get_current_user,
    require_bearer_token,
)
from app.db import get_db, init_db, utc_now_iso
from app.services import (
    AIConnectionError,
    UI_STATUS_MAP,
    add_entry,
    apply_proposal,
    apply_quick_fix,
    create_proposal,
    delete_entry,
    dismiss_proposal,
    execute_punch,
    export_months_pdf,
    generate_inbuilt_ai_proposal,
    get_live_status,
    get_month_data,
    import_uploaded_file,
    normalize_date,
    normalize_ym,
    parse_signed_hm,
    set_day_meta,
    test_llm_connection,
    update_entry,
    user_now,
)
from table_utils import STATUS_MAP

APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Punch!", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# --- Pydantic payloads for JSON APIs ---

class WatchPairRequest(BaseModel):
    code: str
    device_name: Optional[str] = "Wear OS Watch"


class WatchPunchRequest(BaseModel):
    action: str = "toggle"
    location: Optional[str] = None
    client_ts: Optional[str] = None
    event_id: Optional[str] = None


class AgentProposalRequest(BaseModel):
    summary: str
    suspicious_days: List[Dict[str, Any]] = Field(default_factory=list)
    changes: List[Dict[str, Any]] = Field(default_factory=list)


class EntryPatchRequest(BaseModel):
    start_time: str
    end_time: Optional[str] = None
    location: str = "remote"


class DayMetaPutRequest(BaseModel):
    day_code: Optional[str] = None
    note: Optional[str] = None


# --- Helpers ---

def _base_ctx(request: Request, user: Dict[str, Any], active_nav: str, nav_ym: Optional[str] = None) -> Dict[str, Any]:
    now_dt = user_now(user["timezone"])
    current_ym = now_dt.strftime("%Y-%m")
    return {
        "request": request,
        "user": user,
        "active_nav": active_nav,
        "nav_ym": nav_ym or current_ym,
        "current_ym": current_ym,
        "pdf_ts": int(now_dt.timestamp()),
        "flash": request.query_params.get("flash"),
        "flash_kind": request.query_params.get("kind", "ok"),
    }


def _render_settings(
    request: Request,
    db: sqlite3.Connection,
    user: Dict[str, Any],
    *,
    new_pairing_code: Optional[Dict[str, str]] = None,
    created_token: Optional[Dict[str, Any]] = None,
    flash: Optional[str] = None,
    flash_kind: str = "ok",
):
    tokens = db.execute(
        """
        SELECT id, name, scope, last_used_at, revoked_at, created_at
        FROM api_tokens
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user["id"],),
    ).fetchall()
    identities = db.execute(
        """
        SELECT id, provider, external_subject
        FROM user_identities
        WHERE user_id = ?
        ORDER BY id ASC
        """,
        (user["id"],),
    ).fetchall()
    live = get_live_status(db, user)
    ctx = _base_ctx(request, user, "settings")
    ctx.update({
        "live": live,
        "tokens": tokens,
        "identities": identities,
        "new_pairing_code": new_pairing_code,
        "created_token": created_token,
    })
    if flash:
        ctx["flash"] = flash
        ctx["flash_kind"] = flash_kind
    return TEMPLATES.TemplateResponse(request, "settings.html", ctx)


# --- Health & PWA ---

@app.get("/api/health")
def health_check() -> Dict[str, str]:
    return {"status": "ok", "service": "punch"}


@app.get("/manifest.json")
def pwa_manifest() -> JSONResponse:
    return JSONResponse({
        "name": "Punch!",
        "short_name": "Punch!",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f6f7f9",
        "theme_color": "#f6f7f9",
    })


def _safe_redirect_url(redirect_to: Optional[str], fallback: str) -> str:
    if redirect_to:
        cand = redirect_to.strip()
        if cand.startswith("/") and not cand.startswith("//"):
            return cand
    return fallback


# --- Web / UI Endpoints (Authelia-protected) ---

@app.get("/")
def dashboard(
    request: Request,
    ym: Optional[str] = Query(default=None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    live = get_live_status(db, user)
    target_ym = normalize_ym(ym) if ym else user_now(user["timezone"]).strftime("%Y-%m")
    month_data = get_month_data(db, user, target_ym)
    ctx = _base_ctx(request, user, "dashboard", nav_ym=target_ym)
    ctx.update({
        "live": live,
        "month_data": month_data,
        "current_ym": live["today_date"][:7],
        "status_map": UI_STATUS_MAP,
    })
    return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)


@app.post("/punch")
def web_punch(
    action: str = Form("toggle"),
    location: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    execute_punch(db, user, action=action, location=location, source="web")
    return RedirectResponse(url="/", status_code=303)


@app.post("/upload-atoss")
async def upload_atoss(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    content = await file.read()
    res = import_uploaded_file(db, user, file.filename or "upload.pdf", content)
    msg = quote(
        f"Imported {res['inserted_entries']} new, {res['merged_entries']} merged, {res['day_codes_imported']} code(s) from {file.filename}"
    )
    return RedirectResponse(url=f"/months/{res['year_month']}?flash={msg}&kind=ok", status_code=303)


@app.get("/months/{ym}")
def month_view(
    request: Request,
    ym: str,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    live = get_live_status(db, user)
    month_data = get_month_data(db, user, ym)
    ctx = _base_ctx(request, user, "month", nav_ym=month_data["year_month"])
    ctx.update({
        "live": live,
        "month": month_data,
        "status_map": UI_STATUS_MAP,
    })
    return TEMPLATES.TemplateResponse(request, "month.html", ctx)


@app.post("/months/{ym}/entries")
def create_month_entry(
    ym: str,
    work_date: str = Form(...),
    start_time: str = Form(...),
    end_time: Optional[str] = Form(None),
    location: str = Form("remote"),
    redirect_to: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(ym)
    created = add_entry(db, user["id"], work_date, start_time, end_time or None, location, sources=["manual"])
    target = _safe_redirect_url(redirect_to, f"/months/{clean_ym}#day-{created['work_date']}")
    return RedirectResponse(url=target, status_code=303)


@app.patch("/entries/{entry_id}")
def patch_entry(
    entry_id: int,
    payload: EntryPatchRequest,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    updated = update_entry(db, user["id"], entry_id, payload.start_time, payload.end_time, payload.location)
    return JSONResponse(updated)


@app.post("/entries/{entry_id}/update")
def post_update_entry(
    entry_id: int,
    year_month: str = Form(...),
    start_time: str = Form(...),
    end_time: Optional[str] = Form(None),
    location: str = Form("remote"),
    redirect_to: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(year_month)
    updated = update_entry(db, user["id"], entry_id, start_time, end_time, location)
    target = _safe_redirect_url(redirect_to, f"/months/{clean_ym}#day-{updated['work_date']}")
    return RedirectResponse(url=target, status_code=303)


@app.delete("/entries/{entry_id}")
def api_delete_entry(
    entry_id: int,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    delete_entry(db, user["id"], entry_id)
    return JSONResponse({"deleted": True, "id": entry_id})


@app.post("/entries/{entry_id}/delete")
def post_delete_entry(
    entry_id: int,
    year_month: str = Form(...),
    redirect_to: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(year_month)
    deleted_date = delete_entry(db, user["id"], entry_id)
    target = _safe_redirect_url(redirect_to, f"/months/{clean_ym}#day-{deleted_date}")
    return RedirectResponse(url=target, status_code=303)


@app.put("/months/{ym}/days/{date_str}")
def put_day_meta(
    ym: str,
    date_str: str,
    payload: DayMetaPutRequest,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    normalize_ym(ym)
    set_day_meta(db, user["id"], date_str, payload.day_code, payload.note)
    return JSONResponse({"ok": True, "date": date_str})


@app.post("/months/{ym}/days/{date_str}")
def post_day_meta(
    ym: str,
    date_str: str,
    day_code: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    redirect_to: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(ym)
    set_day_meta(db, user["id"], date_str, day_code, note)
    target = _safe_redirect_url(redirect_to, f"/months/{clean_ym}#day-{date_str}")
    return RedirectResponse(url=target, status_code=303)


@app.post("/months/{ym}/quick-fix")
def post_quick_fix(
    ym: str,
    fix_type: str = Form(...),
    date_str: str = Form(...),
    entry_id: Optional[int] = Form(None),
    mode: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(ym)
    apply_quick_fix(db, user, clean_ym, fix_type=fix_type, date_str=date_str, entry_id=entry_id, mode=mode)
    msg = quote(f"Applied {fix_type} on {date_str}")
    return RedirectResponse(url=f"/months/{clean_ym}?flash={msg}#day-{date_str}", status_code=303)


@app.post("/months/{ym}/ai-assist")
async def post_ai_assist(
    ym: str,
    instruction: str = Form(""),
    mode: str = Form("ai"),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_ym = normalize_ym(ym)
    try:
        await generate_inbuilt_ai_proposal(db, user, clean_ym, instruction=instruction, mode=mode)
    except (AIConnectionError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return RedirectResponse(
            url=f"/months/{clean_ym}?flash={quote(str(msg))}&kind=error#ai-assist",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/months/{clean_ym}?flash={quote(f'AI review failed: {exc}')}&kind=error#ai-assist",
            status_code=303,
        )
    msg = quote("Review proposal ready below")
    return RedirectResponse(url=f"/months/{clean_ym}?flash={msg}&kind=ok#proposals", status_code=303)


@app.post("/proposals/{proposal_id}/apply")
def post_apply_proposal(
    proposal_id: int,
    change_ids: List[str] = Form(default=[]),
    accept_all: Optional[str] = Form(default=None),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    selected = None if (accept_all and accept_all.lower() == "true") else change_ids
    res = apply_proposal(db, user["id"], proposal_id, accepted_change_ids=selected)
    msg = quote("Proposal applied")
    return RedirectResponse(url=f"/months/{res['year_month']}?flash={msg}", status_code=303)


@app.post("/proposals/{proposal_id}/dismiss")
def post_dismiss_proposal(
    request: Request,
    proposal_id: int,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute("SELECT year_month FROM proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
    ym = row["year_month"] if row else user_now(user["timezone"]).strftime("%Y-%m")
    dismiss_proposal(db, user["id"], proposal_id)
    return RedirectResponse(url=f"/months/{ym}", status_code=303)


@app.get("/export/pdf")
def export_pdf(
    months: str = Query(...),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    month_list = [m.strip() for m in months.split(",") if m.strip()]
    if not month_list:
        raise HTTPException(status_code=400, detail="At least one YYYY-MM month is required")
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = export_months_pdf(db, user, month_list, Path(tmpdir))
        pdf_bytes = pdf_path.read_bytes()
    safe_name = "_".join(normalize_ym(m) for m in month_list)
    stamp = user_now(user["timezone"]).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="atoss_{safe_name}_{stamp}.pdf"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# --- Settings Routes ---

@app.get("/settings")
def settings_page(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    return _render_settings(request, db, user)


@app.post("/settings/profile")
def update_profile(
    display_name: str = Form(...),
    timezone: str = Form(...),
    target_minutes: int = Form(...),
    verbeamtung_date: str = Form(""),
    balance_anchor_date: str = Form(""),
    balance_anchor_hm: str = Form(""),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    try:
        ZoneInfo(timezone.strip())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid timezone '{timezone}'") from exc

    raw_verb = (verbeamtung_date or "").strip()
    clean_verb_date: Optional[str] = normalize_date(raw_verb) if raw_verb else None

    raw_date = (balance_anchor_date or "").strip()
    raw_hm = (balance_anchor_hm or "").strip()
    if raw_date or raw_hm:
        if not raw_date or not raw_hm:
            raise HTTPException(status_code=400, detail="Both balance anchor date and balance (±HH:MM) are required")
        clean_anchor_date: Optional[str] = normalize_date(raw_date)
        clean_anchor_minutes: Optional[int] = parse_signed_hm(raw_hm)
    else:
        clean_anchor_date = None
        clean_anchor_minutes = None

    db.execute(
        """
        UPDATE users
        SET display_name = ?, timezone = ?, target_minutes = ?,
            verbeamtung_date = ?, balance_anchor_date = ?, balance_anchor_minutes = ?
        WHERE id = ?
        """,
        (
            display_name.strip(),
            timezone.strip(),
            int(target_minutes),
            clean_verb_date,
            clean_anchor_date,
            clean_anchor_minutes,
            user["id"],
        ),
    )
    db.commit()
    msg = quote("Profile updated")
    return RedirectResponse(url=f"/settings?flash={msg}", status_code=303)


@app.post("/settings/llm")
async def update_llm_settings(
    llm_provider: str = Form("gemini"),
    llm_model: str = Form(""),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    clear_api_key: Optional[str] = Form(None),
    action_mode: str = Form("save"),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    clean_provider = (llm_provider or "gemini").strip().lower()
    if clean_provider not in ("gemini", "openai", "anthropic"):
        raise HTTPException(status_code=400, detail=f"Unsupported LLM provider '{llm_provider}'")
    clean_model = llm_model.strip() or None
    clean_base_url = llm_base_url.strip().rstrip("/") or None

    if clear_api_key and clear_api_key.lower() == "true":
        db.execute(
            "UPDATE users SET llm_provider = ?, llm_model = ?, llm_base_url = ?, llm_api_key = NULL WHERE id = ?",
            (clean_provider, clean_model, clean_base_url, user["id"]),
        )
    elif llm_api_key.strip():
        db.execute(
            "UPDATE users SET llm_provider = ?, llm_model = ?, llm_base_url = ?, llm_api_key = ? WHERE id = ?",
            (clean_provider, clean_model, clean_base_url, llm_api_key.strip(), user["id"]),
        )
    else:
        db.execute(
            "UPDATE users SET llm_provider = ?, llm_model = ?, llm_base_url = ? WHERE id = ?",
            (clean_provider, clean_model, clean_base_url, user["id"]),
        )
    db.commit()

    if action_mode == "test":
        row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        updated_user = _row_to_user(row)
        ok, test_msg = await test_llm_connection(updated_user)
        kind = "ok" if ok else "error"
        return RedirectResponse(url=f"/settings?flash={quote(test_msg)}&kind={kind}", status_code=303)

    msg = quote("AI settings saved")
    return RedirectResponse(url=f"/settings?flash={msg}&kind=ok", status_code=303)


@app.post("/settings/pairing-code")
def settings_pairing_code(
    request: Request,
    device_name: str = Form("Wear OS Watch"),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    code_info = create_pairing_code(db, user["id"], device_name=device_name)
    return _render_settings(request, db, user, new_pairing_code=code_info)


@app.post("/settings/tokens")
def settings_create_token(
    request: Request,
    name: str = Form(...),
    scope: str = Form(...),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    token_info = create_api_token(db, user["id"], name=name, scope=scope)
    return _render_settings(request, db, user, created_token=token_info)


@app.post("/settings/tokens/{token_id}/revoke")
def settings_revoke_token(
    token_id: int,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    db.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ?",
        (utc_now_iso(), token_id, user["id"]),
    )
    db.commit()
    return RedirectResponse(url="/settings?flash=Token+revoked", status_code=303)


@app.post("/settings/identities")
def settings_link_identity(
    external_subject: str = Form(...),
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    subj = external_subject.strip()
    if not subj:
        raise HTTPException(status_code=400, detail="Identity cannot be empty")
    try:
        db.execute(
            """
            INSERT INTO user_identities (user_id, provider, external_subject)
            VALUES (?, 'authelia', ?)
            """,
            (user["id"], subj),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Authelia identity already linked") from exc
    return RedirectResponse(url="/settings?flash=Identity+linked", status_code=303)


@app.post("/settings/identities/{identity_id}/delete")
def settings_unlink_identity(
    identity_id: int,
    user: Dict[str, Any] = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    count = db.execute("SELECT COUNT(*) FROM user_identities WHERE user_id = ?", (user["id"],)).fetchone()[0]
    if count <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last Authelia identity")
    db.execute("DELETE FROM user_identities WHERE id = ? AND user_id = ?", (identity_id, user["id"]))
    db.commit()
    return RedirectResponse(url="/settings?flash=Identity+removed", status_code=303)


# --- Wear OS API (/api/watch/*, Bearer token) ---

def _watch_status_payload(live: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "clocked_in": live["clocked_in"],
        "active_entry": live["active_entry"],
        "today_completed_minutes": live["today_completed_minutes"],
        "break_required_minutes": live["break_required_minutes"],
        "break_actual_minutes": live["break_actual_minutes"],
        "warnings": live["warnings"],
        "last_location": live["last_location"],
    }


@app.post("/api/watch/pair")
def watch_pair(
    payload: WatchPairRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    res = exchange_pairing_code(db, payload.code, device_name=payload.device_name)
    return JSONResponse(res)


@app.get("/api/watch/status")
def watch_status(
    user: Dict[str, Any] = Depends(require_bearer_token("watch")),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    live = get_live_status(db, user)
    return JSONResponse(_watch_status_payload(live))


@app.post("/api/watch/punch")
def watch_punch(
    payload: WatchPunchRequest,
    user: Dict[str, Any] = Depends(require_bearer_token("watch")),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    live = execute_punch(
        db,
        user,
        action=payload.action,
        location=payload.location,
        client_ts=payload.client_ts,
        event_id=payload.event_id,
        source="watch",
    )
    return JSONResponse(_watch_status_payload(live))


# --- External Agent API (/api/agent/*, Bearer token) ---

@app.get("/api/agent/months/{ym}")
def agent_get_month(
    ym: str,
    user: Dict[str, Any] = Depends(require_bearer_token("agent")),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    month_data = get_month_data(db, user, ym)
    return JSONResponse({
        "year_month": month_data["year_month"],
        "month_label": month_data["month_label"],
        "worked_minutes": month_data["worked_minutes"],
        "expected_minutes": month_data["expected_minutes"],
        "balance_delta_minutes": month_data["balance_delta_minutes"],
        "entries": month_data["entries"],
        "day_codes": month_data["day_codes"],
        "day_notes": month_data["day_notes"],
        "hard_issues": month_data["hard_issues"],
        "soft_anomalies": month_data["soft_anomalies"],
        "empty_workdays": month_data["empty_workdays"],
        "pending_proposals": month_data["pending_proposals"],
    })


@app.post("/api/agent/months/{ym}/proposals")
def agent_create_proposal(
    ym: str,
    payload: AgentProposalRequest,
    user: Dict[str, Any] = Depends(require_bearer_token("agent")),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    prop = create_proposal(
        db,
        user_id=user["id"],
        year_month=ym,
        source="external_agent",
        summary=payload.summary,
        suspicious_days=payload.suspicious_days,
        changes=payload.changes,
    )
    return JSONResponse(prop, status_code=201)
