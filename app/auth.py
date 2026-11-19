from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from app.db import get_db, utc_now_iso


def _trust_proxy_headers() -> bool:
    return os.environ.get("TRUST_PROXY_HEADERS", "true").strip().lower() in ("1", "true", "yes", "on")


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    llm_key = row["llm_api_key"] if "llm_api_key" in keys else None
    llm_provider = row["llm_provider"] if "llm_provider" in keys and row["llm_provider"] else "gemini"
    llm_model = row["llm_model"] if "llm_model" in keys else None
    llm_base_url = row["llm_base_url"] if "llm_base_url" in keys else None
    anchor_date = row["balance_anchor_date"] if "balance_anchor_date" in keys else None
    anchor_minutes = row["balance_anchor_minutes"] if "balance_anchor_minutes" in keys else None
    verbeamtung_date = row["verbeamtung_date"] if "verbeamtung_date" in keys else None
    anchor_fmt = None
    if anchor_minutes is not None:
        val = int(anchor_minutes)
        sign = "-" if val < 0 else "+"
        aval = abs(val)
        anchor_fmt = f"{sign}{aval // 60:02d}:{aval % 60:02d}"
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "timezone": row["timezone"],
        "target_minutes": int(row["target_minutes"]),
        "llm_provider": llm_provider,
        "llm_api_key": llm_key,
        "llm_model": llm_model,
        "llm_base_url": llm_base_url,
        "has_llm_api_key": bool(llm_key),
        "has_llm_configured": bool(llm_key or (llm_provider == "openai" and llm_base_url)),
        "balance_anchor_date": anchor_date,
        "balance_anchor_minutes": int(anchor_minutes) if anchor_minutes is not None else None,
        "balance_anchor_fmt": anchor_fmt,
        "verbeamtung_date": verbeamtung_date,
        "created_at": row["created_at"],
    }


def _find_user_by_identity(
    db: sqlite3.Connection,
    subjects: list[str],
    provider: str = "authelia",
) -> Optional[Dict[str, Any]]:
    for subj in subjects:
        clean = (subj or "").strip()
        if not clean:
            continue
        row = db.execute(
            """
            SELECT u.*
            FROM user_identities ui
            JOIN users u ON u.id = ui.user_id
            WHERE ui.provider = ? AND lower(ui.external_subject) = lower(?)
            LIMIT 1
            """,
            (provider, clean),
        ).fetchone()
        if row is not None:
            return _row_to_user(row)
    return None


def _get_or_provision_user(
    db: sqlite3.Connection,
    username: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    link_authelia: bool = True,
) -> Dict[str, Any]:
    clean_username = username.strip().lower()
    now = utc_now_iso()
    row = db.execute("SELECT * FROM users WHERE lower(username) = ?", (clean_username,)).fetchone()
    if row is None:
        cur = db.execute(
            """
            INSERT INTO users (username, display_name, timezone, target_minutes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                clean_username,
                (display_name or username).strip(),
                os.environ.get("TZ", "Europe/Berlin"),
                492,
                now,
            ),
        )
        user_id = int(cur.lastrowid)
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    else:
        user_id = int(row["id"])

    if link_authelia:
        db.execute(
            """
            INSERT OR IGNORE INTO user_identities (user_id, provider, external_subject)
            VALUES (?, 'authelia', ?)
            """,
            (user_id, username.strip()),
        )
        if email and email.strip():
            db.execute(
                """
                INSERT OR IGNORE INTO user_identities (user_id, provider, external_subject)
                VALUES (?, 'authelia', ?)
                """,
                (user_id, email.strip()),
            )
    db.commit()
    return _row_to_user(row)


def get_current_user(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> Dict[str, Any]:
    if _trust_proxy_headers():
        remote_user = (request.headers.get("Remote-User") or "").strip()
        remote_email = (request.headers.get("Remote-Email") or "").strip()
        remote_name = (request.headers.get("Remote-Name") or "").strip()

        if remote_user or remote_email:
            found = _find_user_by_identity(db, [remote_user, remote_email], provider="authelia")
            if found is not None:
                return found
            if remote_user:
                return _get_or_provision_user(
                    db,
                    username=remote_user,
                    display_name=remote_name or remote_user,
                    email=remote_email or None,
                    link_authelia=True,
                )
            if remote_email:
                fallback_uname = remote_email.split("@", 1)[0]
                return _get_or_provision_user(
                    db,
                    username=fallback_uname,
                    display_name=remote_name or fallback_uname,
                    email=remote_email,
                    link_authelia=True,
                )

    dev_user = os.environ.get("DEV_USER", "").strip()
    if dev_user:
        return _get_or_provision_user(db, username=dev_user, display_name=dev_user, link_authelia=False)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required (missing Authelia Remote-User header)",
    )


def require_bearer_token(scope: str) -> Callable[..., Dict[str, Any]]:
    def _dep(
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
        db: sqlite3.Connection = Depends(get_db),
    ) -> Dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raw_token = authorization.split(" ", 1)[1].strip()
        if not raw_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        thash = hash_token(raw_token)
        row = db.execute(
            """
            SELECT t.id AS token_id, u.*
            FROM api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
              AND t.scope = ?
              AND t.revoked_at IS NULL
            LIMIT 1
            """,
            (thash, scope),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked token for scope",
                headers={"WWW-Authenticate": "Bearer"},
            )
        db.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (utc_now_iso(), row["token_id"]),
        )
        db.commit()
        return _row_to_user(row)

    return _dep


def create_pairing_code(
    db: sqlite3.Connection,
    user_id: int,
    device_name: Optional[str] = None,
    ttl_minutes: int = 5,
) -> Dict[str, str]:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).replace(microsecond=0).isoformat()
    for _ in range(10):
        code = f"{secrets.randbelow(900000) + 100000}"
        existing = db.execute("SELECT 1 FROM pairing_codes WHERE code = ?", (code,)).fetchone()
        if existing is None:
            db.execute(
                """
                INSERT INTO pairing_codes (code, user_id, device_name, expires_at, used_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (code, user_id, (device_name or "Wear OS Watch").strip(), expires_at),
            )
            db.commit()
            return {"code": code, "expires_at": expires_at}
    raise RuntimeError("Failed to generate unique pairing code")


def create_api_token(
    db: sqlite3.Connection,
    user_id: int,
    name: str,
    scope: str,
) -> Dict[str, Any]:
    if scope not in ("watch", "agent"):
        raise ValueError("scope must be 'watch' or 'agent'")
    prefix = "atoss_w_" if scope == "watch" else "atoss_a_"
    raw_token = prefix + secrets.token_urlsafe(32)
    now = utc_now_iso()
    cur = db.execute(
        """
        INSERT INTO api_tokens (user_id, name, scope, token_hash, last_used_at, revoked_at, created_at)
        VALUES (?, ?, ?, ?, NULL, NULL, ?)
        """,
        (user_id, name.strip() or f"{scope}-token", scope, hash_token(raw_token), now),
    )
    db.commit()
    return {
        "id": int(cur.lastrowid),
        "name": name.strip() or f"{scope}-token",
        "scope": scope,
        "token": raw_token,
        "created_at": now,
    }


def exchange_pairing_code(
    db: sqlite3.Connection,
    code: str,
    device_name: Optional[str] = None,
) -> Dict[str, Any]:
    clean_code = (code or "").strip()
    row = db.execute(
        """
        SELECT p.*, u.id AS uid, u.username, u.display_name, u.timezone, u.target_minutes
        FROM pairing_codes p
        JOIN users u ON u.id = p.user_id
        WHERE p.code = ?
        LIMIT 1
        """,
        (clean_code,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid pairing code")
    if row["used_at"] is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pairing code already used")

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pairing code expired")

    now = utc_now_iso()
    db.execute("UPDATE pairing_codes SET used_at = ? WHERE code = ?", (now, clean_code))
    token_name = (device_name or row["device_name"] or "Wear OS Watch").strip()
    token_info = create_api_token(db, int(row["uid"]), name=token_name, scope="watch")
    return {
        "token": token_info["token"],
        "user": {
            "id": int(row["uid"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "timezone": row["timezone"],
        },
    }
