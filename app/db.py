from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from table_utils import parse_report_entries

REPO_ROOT = Path(__file__).resolve().parent.parent


def get_database_path() -> Path:
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return Path(env_path)
    for prod_dir, db_name in ((Path("/var/lib/punch"), "punch.db"), (Path("/var/lib/atoss"), "atoss.db")):
        if prod_dir.exists() and os.access(prod_dir, os.W_OK):
            return prod_dir / db_name
    return REPO_ROOT / "punch.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect_db()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT,
    timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
    target_minutes INTEGER NOT NULL DEFAULT 492,
    llm_provider TEXT NOT NULL DEFAULT 'gemini',
    llm_api_key TEXT,
    llm_model TEXT,
    llm_base_url TEXT,
    balance_anchor_date TEXT,
    balance_anchor_minutes INTEGER,
    verbeamtung_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'authelia',
    external_subject TEXT NOT NULL,
    UNIQUE(provider, external_subject)
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name TEXT,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('watch', 'agent')),
    token_hash TEXT UNIQUE NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    location TEXT NOT NULL DEFAULT 'remote',
    sources_json TEXT NOT NULL,
    client_event_id TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, client_event_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, work_date);

CREATE TABLE IF NOT EXISTS punch_events (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, event_id)
);

CREATE TABLE IF NOT EXISTS day_meta (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL,
    day_code TEXT,
    note TEXT,
    PRIMARY KEY(user_id, work_date)
);

CREATE TABLE IF NOT EXISTS atoss_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year_month TEXT NOT NULL,
    filename TEXT NOT NULL,
    parsed_summary_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year_month TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('inbuilt_ai', 'external_agent')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'applied', 'dismissed')),
    summary TEXT NOT NULL,
    suspicious_days_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def _resolve_seed_paths() -> list[Path]:
    env_paths = os.environ.get("SEED_REPORT_PATHS", "").strip()
    if not env_paths:
        return []
    return [Path(p.strip()) for p in env_paths.split(",") if p.strip()]


def seed_initial_data(conn: sqlite3.Connection) -> None:
    seed_paths = [p for p in _resolve_seed_paths() if p.exists()]
    seed_username = os.environ.get("SEED_USERNAME", "").strip().lower()
    if not seed_paths and not seed_username:
        return

    now = utc_now_iso()
    default_tz = os.environ.get("TZ", "Europe/Berlin")
    username = seed_username or "user"
    display_name = os.environ.get("SEED_DISPLAY_NAME", "").strip() or username.title()

    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO users (username, display_name, timezone, target_minutes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, display_name, default_tz, 492, now),
        )
        user_id = int(cur.lastrowid)
    else:
        user_id = int(row["id"])

    subjects = [username]
    seed_email = os.environ.get("SEED_EMAIL", "").strip()
    if seed_email:
        subjects.append(seed_email)
    for subject in subjects:
        conn.execute(
            """
            INSERT OR IGNORE INTO user_identities (user_id, provider, external_subject)
            VALUES (?, 'authelia', ?)
            """,
            (user_id, subject),
        )

    existing_any = conn.execute(
        "SELECT 1 FROM entries WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()

    if existing_any is None:
        for seed_path in seed_paths:
            parsed = parse_report_entries(seed_path)
            for item in parsed.get("entries", []):
                work_date = str(item.get("date", ""))
                if not work_date:
                    continue
                raw_source = str(item.get("source") or "manual")
                sources = [s.strip() for s in raw_source.split(",") if s.strip()] or ["manual"]
                location = str(item.get("location") or "-")
                conn.execute(
                    """
                    INSERT INTO entries (
                        user_id, work_date, start_time, end_time, location,
                        sources_json, client_event_id, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        user_id,
                        work_date,
                        item["start"],
                        item["end"],
                        location,
                        json.dumps(sources),
                        now,
                        now,
                    ),
                )

            all_dates = set(parsed.get("day_codes", {}).keys()) | set(parsed.get("day_notes", {}).keys())
            for work_date in sorted(all_dates):
                code = parsed.get("day_codes", {}).get(work_date)
                note = parsed.get("day_notes", {}).get(work_date)
                conn.execute(
                    """
                    INSERT INTO day_meta (user_id, work_date, day_code, note)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, work_date) DO UPDATE SET
                        day_code = COALESCE(excluded.day_code, day_meta.day_code),
                        note = COALESCE(excluded.note, day_meta.note)
                    """,
                    (user_id, work_date, code, note),
                )

    conn.commit()


def _migrate_db(conn: sqlite3.Connection) -> None:
    existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "llm_provider" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN llm_provider TEXT NOT NULL DEFAULT 'gemini'")
    if "llm_api_key" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN llm_api_key TEXT")
    if "llm_model" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN llm_model TEXT")
    if "llm_base_url" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN llm_base_url TEXT")
    if "balance_anchor_date" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN balance_anchor_date TEXT")
    if "balance_anchor_minutes" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN balance_anchor_minutes INTEGER")
    if "verbeamtung_date" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN verbeamtung_date TEXT")
    conn.commit()


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect_db(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_db(conn)
        seed_initial_data(conn)
    finally:
        conn.close()
