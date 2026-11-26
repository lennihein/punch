from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.main import app  # noqa: E402


SAMPLE_SEP_REPORT = """Month: September 2026
Generated: 2026-09-21

2026-09-01 (Tuesday)
  Total worked: 07:48 (delta 00:00)
    start  end    duration  location  source  issues
    -----  -----  --------  --------  ------  ------
    09:00  12:30  03:30     remote    app
    13:00  17:18  04:18     remote    app

2026-09-02 (Wednesday)
  Total worked: 07:48 (delta 00:00)
    start  end    duration  location  source  issues
    -----  -----  --------  --------  ------  ------
    08:30  12:00  03:30     office    manual
    12:30  16:48  04:18     remote    app

2026-09-03 (Thursday)
  Total worked: 07:48 (delta 00:00)
    start  end    duration  location  source  issues
    -----  -----  --------  --------  ------  ------
    08:00  12:00  04:00     remote    manual
    12:30  16:18  03:48     remote    manual
"""


def _write_sample_report(tmp_path: Path) -> Path:
    sample = tmp_path / "sample_sep_2026.txt"
    sample.write_text(SAMPLE_SEP_REPORT, encoding="utf-8")
    return sample


def _make_client(monkeypatch, tmp_path: Path, seed_sep: bool = True) -> TestClient:
    db_file = tmp_path / "test_atoss.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SEED_USERNAME", "alice")
    monkeypatch.setenv("SEED_DISPLAY_NAME", "Alice")
    monkeypatch.setenv("SEED_EMAIL", "alice@example.com")
    if seed_sep:
        monkeypatch.setenv("SEED_REPORT_PATHS", str(_write_sample_report(tmp_path)))
    else:
        monkeypatch.setenv("SEED_REPORT_PATHS", str(tmp_path / "nonexistent.txt"))
    return TestClient(app)


def test_seeding_and_authelia_multi_identity(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        # Unauthenticated fails with 401
        r_unauth = client.get("/api/agent/months/2026-09")
        assert r_unauth.status_code == 401

        r_web_unauth = client.get("/")
        assert r_web_unauth.status_code == 401

        # Authenticated via primary Authelia user
        r_sep = client.get("/months/2026-09", headers={"Remote-User": "alice"})
        assert r_sep.status_code == 200
        assert "September 2026" in r_sep.text
        assert "09:00" in r_sep.text

        # Secondary Authelia email maps to the exact same user and sees seeded entries
        r_alt = client.get("/months/2026-09", headers={"Remote-Email": "alice@example.com"})
        assert r_alt.status_code == 200
        assert "09:00" in r_alt.text

        # August 2026 must NOT be seeded
        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        aug_cnt = conn.execute("SELECT COUNT(*) FROM entries WHERE work_date LIKE '2026-08-%'").fetchone()[0]
        sep_cnt = conn.execute("SELECT COUNT(*) FROM entries WHERE work_date LIKE '2026-09-%'").fetchone()[0]
        conn.close()
        assert aug_cnt == 0
        assert sep_cnt == 6


def test_pwa_installability_assets(monkeypatch, tmp_path: Path):
    import re

    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        # Manifest must satisfy Chrome's installability checks: the canonical
        # manifest media type, standalone display, start_url, and 192px + 512px
        # PNG icons that are actually fetchable.
        r = client.get("/manifest.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        m = r.json()
        assert m["display"] == "standalone"
        assert m["start_url"] == "/"
        assert m.get("scope") == "/"
        sizes = {i["sizes"] for i in m["icons"]}
        assert "192x192" in sizes
        assert "512x512" in sizes
        for icon in m["icons"]:
            ri = client.get(icon["src"])
            assert ri.status_code == 200, icon["src"]

        # Manifest theme/background colors must match the light theme used by
        # the page (base.html meta theme-color / style.css --bg).
        assert m["theme_color"] == "#f2f0eb"
        assert m["background_color"] == "#f2f0eb"

        # A service worker with a fetch handler must be served from root scope.
        sw = client.get("/sw.js")
        assert sw.status_code == 200
        assert "javascript" in sw.headers["content-type"]
        assert sw.headers.get("Service-Worker-Allowed") == "/"
        assert "addEventListener('fetch'" in sw.text

        # Pages must register the service worker, link the manifest with
        # credentials (the site sits behind Authelia cookie auth), and carry a
        # theme-color meta consistent with the manifest.
        page = client.get("/", headers={"Remote-User": "alice"})
        assert page.status_code == 200
        assert "navigator.serviceWorker.register('/sw.js')" in page.text
        assert '<link rel="manifest" href="/manifest.json" crossorigin="use-credentials">' in page.text
        meta = re.search(r'<meta name="theme-color" content="([^"]+)"', page.text)
        assert meta is not None
        assert meta.group(1) == m["theme_color"]


def test_web_punch_flow_and_location_switch(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}
        # Clock in remote
        r1 = client.post("/punch", data={"action": "in", "location": "remote"}, headers=headers, follow_redirects=False)
        assert r1.status_code == 303

        r_dash = client.get("/", headers=headers)
        assert r_dash.status_code == 200
        assert "Clocked in · remote" in r_dash.text
        # While clocked in the dashboard shows only the Clock Out button — no
        # switch-to-office/remote button.
        assert "Clock Out" in r_dash.text
        assert "Switch to Office" not in r_dash.text
        assert "Switch to Remote" not in r_dash.text

        # Switch to office (the /punch route still accepts switch_location)
        r2 = client.post("/punch", data={"action": "switch_location", "location": "office"}, headers=headers, follow_redirects=False)
        assert r2.status_code == 303
        r_dash2 = client.get("/", headers=headers)
        assert "Clocked in · office" in r_dash2.text
        assert "Switch to Office" not in r_dash2.text
        assert "Switch to Remote" not in r_dash2.text

        # Clock out
        r3 = client.post("/punch", data={"action": "out"}, headers=headers, follow_redirects=False)
        assert r3.status_code == 303
        r_dash3 = client.get("/", headers=headers)
        assert "Clocked out" in r_dash3.text
        # Clocked out: no Clock Out button, just the two Clock In buttons.
        assert "Clock Out" not in r_dash3.text
        assert "Clock In (Remote)" in r_dash3.text
        assert "Clock In (Office)" in r_dash3.text


def test_watch_pairing_and_idempotent_punch(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        # Generate 6-digit pairing code in Settings
        r_code = client.post("/settings/pairing-code", data={"device_name": "Pixel Watch"}, headers={"Remote-User": "alice"})
        assert r_code.status_code == 200

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        code = conn.execute("SELECT code FROM pairing_codes ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        conn.close()
        assert len(code) == 6 and code.isdigit()

        # Pair watch via public /api/watch/pair
        r_pair = client.post("/api/watch/pair", json={"code": code, "device_name": "Pixel Watch"})
        assert r_pair.status_code == 200
        watch_token = r_pair.json()["token"]
        assert watch_token.startswith("atoss_w_")

        # Code cannot be reused
        r_reuse = client.post("/api/watch/pair", json={"code": code})
        assert r_reuse.status_code == 400

        auth_hdr = {"Authorization": f"Bearer {watch_token}"}

        # Initial watch status
        r_stat = client.get("/api/watch/status", headers=auth_hdr)
        assert r_stat.status_code == 200
        assert r_stat.json()["clocked_in"] is False

        # Punches are stamped today: parse_client_ts only trusts client timestamps
        # within +-7 days of now, so a fixed date would age out of that window.
        from datetime import datetime, time
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(tz).date()

        # Idempotent punch-in (same event_id sent twice -> 1 row)
        payload_in = {
            "action": "in",
            "location": "remote",
            "client_ts": datetime.combine(today, time(8, 30), tzinfo=tz).isoformat(),
            "event_id": "evt-in-001",
        }
        p1 = client.post("/api/watch/punch", json=payload_in, headers=auth_hdr)
        p2 = client.post("/api/watch/punch", json=payload_in, headers=auth_hdr)
        assert p1.status_code == 200 and p2.status_code == 200
        assert p1.json()["clocked_in"] is True
        assert p2.json()["active_entry"]["start"] == "08:30"

        # Idempotent toggle punch-out (same event_id sent twice -> remains clocked out!)
        payload_out = {
            "action": "toggle",
            "location": "remote",
            "client_ts": datetime.combine(today, time(12, 15), tzinfo=tz).isoformat(),
            "event_id": "evt-out-002",
        }
        po1 = client.post("/api/watch/punch", json=payload_out, headers=auth_hdr)
        po2 = client.post("/api/watch/punch", json=payload_out, headers=auth_hdr)
        assert po1.status_code == 200 and po2.status_code == 200
        assert po1.json()["clocked_in"] is False
        assert po2.json()["clocked_in"] is False

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        cnt = conn.execute("SELECT COUNT(*) FROM entries WHERE deleted_at IS NULL").fetchone()[0]
        conn.close()
        assert cnt == 1


def test_quick_fixes_insert_break_and_rahmenzeit(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}
        # Add 08:00 - 16:00 (8h uninterrupted -> missing 30m break)
        client.post(
            "/months/2026-10/entries",
            data={"work_date": "2026-10-12", "start_time": "08:00", "end_time": "16:00", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )
        # Add 05:30 - 08:30 (outside Rahmenzeit 06:00-20:00)
        client.post(
            "/months/2026-10/entries",
            data={"work_date": "2026-10-13", "start_time": "05:30", "end_time": "08:30", "location": "office"},
            headers=headers,
            follow_redirects=False,
        )

        # Quick-fix insert_break on 2026-10-12
        rf1 = client.post(
            "/months/2026-10/quick-fix",
            data={"fix_type": "insert_break", "date_str": "2026-10-12"},
            headers=headers,
            follow_redirects=False,
        )
        assert rf1.status_code == 303

        # Quick-fix shift_rahmenzeit on 2026-10-13
        rf2 = client.post(
            "/months/2026-10/quick-fix",
            data={"fix_type": "shift_rahmenzeit", "date_str": "2026-10-13"},
            headers=headers,
            follow_redirects=False,
        )
        assert rf2.status_code == 303

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        oct12 = conn.execute(
            "SELECT start_time, end_time FROM entries WHERE work_date = '2026-10-12' AND deleted_at IS NULL ORDER BY start_time"
        ).fetchall()
        oct13 = conn.execute(
            "SELECT start_time, end_time FROM entries WHERE work_date = '2026-10-13' AND deleted_at IS NULL ORDER BY start_time"
        ).fetchall()
        conn.close()

        assert oct12 == [("08:00", "12:00"), ("12:30", "16:30")]
        assert oct13 == [("06:00", "09:00")]


def test_agent_api_and_inbuilt_ai_proposal_apply(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        web_headers = {"Remote-User": "alice"}
        # Create agent token via Settings
        r_tok = client.post("/settings/tokens", data={"name": "Antigravity", "scope": "agent"}, headers=web_headers)
        assert r_tok.status_code == 200
        # Extract token from HTML
        import re
        m = re.search(r"atoss_a_[A-Za-z0-9_-]+", r_tok.text)
        assert m is not None
        agent_token = m.group(0)
        agent_headers = {"Authorization": f"Bearer {agent_token}"}

        # GET /api/agent/months/2026-09
        r_month = client.get("/api/agent/months/2026-09", headers=agent_headers)
        assert r_month.status_code == 200
        data = r_month.json()
        assert data["year_month"] == "2026-09"
        assert len(data["entries"]) == 6

        # Submit proposal from external agent
        r_prop = client.post(
            "/api/agent/months/2026-09/proposals",
            headers=agent_headers,
            json={
                "summary": "Mark 2026-09-22 as Urlaub",
                "suspicious_days": [
                    {"date": "2026-09-02", "reason": "Only 02:59 logged", "suggested_check": "Verify Calendar"}
                ],
                "changes": [
                    {
                        "op": "set_day_code",
                        "date": "2026-09-22",
                        "day_code": "U",
                        "reason": "Mark empty day 2026-09-22 as Urlaub",
                    }
                ],
            },
        )
        assert r_prop.status_code == 201
        prop_id = r_prop.json()["id"]

        # Verify pending proposal renders in Month Editor
        r_view = client.get("/months/2026-09", headers=web_headers)
        assert "Mark 2026-09-22 as Urlaub" in r_view.text

        # Apply proposal
        r_apply = client.post(
            f"/proposals/{prop_id}/apply",
            data={"accept_all": "true"},
            headers=web_headers,
            follow_redirects=False,
        )
        assert r_apply.status_code == 303

        # Without an API key, mode=ai warns explicitly instead of silently falling back
        r_ai_missing = client.post(
            "/months/2026-09/ai-assist",
            data={"instruction": "2026-09-23 was sick", "mode": "ai"},
            headers=web_headers,
            follow_redirects=False,
        )
        assert r_ai_missing.status_code == 303
        assert "kind=error" in r_ai_missing.headers["location"]
        assert "No%20AI%20API%20key%20configured" in r_ai_missing.headers["location"]

        # Explicit Offline Rule Scan (mode=heuristic) works without an AI key
        r_ai = client.post(
            "/months/2026-09/ai-assist",
            data={"instruction": "2026-09-23 was sick", "mode": "heuristic"},
            headers=web_headers,
            follow_redirects=False,
        )
        assert r_ai.status_code == 303
        r_view2 = client.get("/months/2026-09", headers=web_headers)
        assert "2026-09-23" in r_view2.text
        assert "Krank" in r_view2.text or "Offline Rule Scan" in r_view2.text


def test_new_proposal_supersedes_pending_and_groups_suspicious_days(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        import re

        web_headers = {"Remote-User": "alice"}
        r_tok = client.post("/settings/tokens", data={"name": "Supersede", "scope": "agent"}, headers=web_headers)
        assert r_tok.status_code == 200
        agent_token = re.search(r"atoss_a_[A-Za-z0-9_-]+", r_tok.text).group(0)
        agent_headers = {"Authorization": f"Bearer {agent_token}"}

        def post_proposal(summary: str, suspicious_days: list) -> int:
            r = client.post(
                "/api/agent/months/2026-09/proposals",
                headers=agent_headers,
                json={"summary": summary, "suspicious_days": suspicious_days, "changes": []},
            )
            assert r.status_code == 201
            return r.json()["id"]

        first_id = post_proposal("First review", [])
        second_id = post_proposal(
            "Second review",
            [
                {"date": "2026-09-01", "reason": "Missing workday", "suggested_check": "Ask employee"},
                {"date": "2026-09-02", "reason": "Missing workday", "suggested_check": "Ask employee"},
                {"date": "2026-09-03", "reason": "Missing workday", "suggested_check": "Ask employee"},
            ],
        )

        data = client.get("/api/agent/months/2026-09", headers=agent_headers).json()
        pending = data["pending_proposals"]
        assert [p["id"] for p in pending] == [second_id]
        assert pending[0]["suspicious_groups"] == [
            {
                "reason": "Missing workday",
                "suggested_check": "Ask employee",
                "dates": ["2026-09-01", "2026-09-02", "2026-09-03"],
                "count": 3,
                "first_date": "2026-09-01",
                "date_label": "2026-09-01 – 2026-09-03 (3 days)",
            }
        ]

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        try:
            statuses = dict(
                conn.execute("SELECT id, status FROM proposals WHERE year_month = '2026-09'").fetchall()
            )
        finally:
            conn.close()
        assert statuses[first_id] == "dismissed"
        assert statuses[second_id] == "pending"


def test_atoss_pdf_upload_and_typst_pdf_export(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}
        sample_txt = _write_sample_report(tmp_path)
        with sample_txt.open("rb") as f:
            r_up = client.post(
                "/upload-atoss",
                files={"file": ("sample_sep_2026.txt", f, "text/plain")},
                headers=headers,
                follow_redirects=False,
            )
        assert r_up.status_code == 303
        assert "/months/2026-09" in r_up.headers["location"]

        r_export = client.get("/export/pdf?months=2026-09", headers=headers)
        assert r_export.status_code == 200
        assert r_export.headers["content-type"].startswith("application/pdf")
        assert r_export.content.startswith(b"%PDF-")


def test_cli_verify_and_pdf_regression(tmp_path: Path):
    sample_txt = _write_sample_report(tmp_path)
    out_txt = tmp_path / "verified_sep26.txt"
    out_pdf = tmp_path / "verified_sep26.pdf"

    res_verify = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "verify", str(sample_txt), "-o", str(out_txt)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_verify.returncode == 0, res_verify.stderr
    assert out_txt.exists()

    res_pdf = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "pdf", str(out_txt), "-o", str(out_pdf)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_pdf.returncode == 0, res_pdf.stderr
    assert out_pdf.exists() and out_pdf.read_bytes().startswith(b"%PDF-")


def test_demo_month_generator_round_trips(tmp_path: Path):
    from demo_data import build_demo_month, demo_filename, normalize_month
    from table_utils import parse_report_entries, render_reviewed_report
    from validation import merge_entries, validate

    assert normalize_month("2026-11") == "2026-11"
    assert normalize_month("2026-11-01") == "2026-11"
    assert normalize_month("nonsense") is None
    assert demo_filename("2026-11") == "demo_november_26.txt"

    bundle = build_demo_month("2026-11", seed=3)
    assert bundle["entries"], "generator produced no intervals"
    assert bundle["day_codes"], "generator produced no day codes"

    merged = merge_entries(bundle["entries"], extra_day_codes=bundle["day_codes"])
    result = validate(merged, extra_day_codes=bundle["day_codes"])
    assert result["issues"], "mixed profile should contain rule violations"

    report_text = render_reviewed_report(
        result, day_notes=bundle["day_notes"], day_codes=bundle["day_codes"]
    )
    report_path = tmp_path / demo_filename("2026-11")
    report_path.write_text(report_text, encoding="utf-8")

    parsed = parse_report_entries(report_path)
    assert len(parsed["entries"]) == len(bundle["entries"])
    assert parsed["day_codes"] == bundle["day_codes"]
    assert parsed["day_notes"] == bundle["day_notes"]

    # Same seed reproduces the month, a different seed does not.
    assert build_demo_month("2026-11", seed=3)["entries"] == bundle["entries"]
    assert build_demo_month("2026-11", seed=4)["entries"] != bundle["entries"]


def test_ai_assist_ajax_returns_rendered_proposal(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        headers = {"Remote-User": "alice", "X-Requested-With": "fetch"}

        r_scan = client.post(
            "/months/2026-09/ai-assist",
            data={"instruction": "Review September", "mode": "heuristic"},
            headers=headers,
        )
        assert r_scan.status_code == 200
        payload = r_scan.json()
        assert payload["ok"] is True
        assert payload["proposal_id"]
        assert "Offline Rule Scan" in payload["proposals_html"]
        assert 'id="proposals"' in payload["proposals_html"]
        assert payload["changes"] + payload["suspicious_days"] > 0

        # Failure is reported as JSON instead of a redirect, so the UI can show it inline.
        r_err = client.post("/months/2026-09/ai-assist", data={"mode": "ai"}, headers=headers)
        assert r_err.status_code == 400
        assert r_err.json()["ok"] is False
        assert "No AI API key" in r_err.json()["error"]

        # Without the fetch header the classic redirect flow still works.
        r_plain = client.post(
            "/months/2026-09/ai-assist",
            data={"mode": "heuristic"},
            headers={"Remote-User": "alice"},
            follow_redirects=False,
        )
        assert r_plain.status_code == 303
        assert "kind=ok" in r_plain.headers["location"]


def test_per_user_llm_settings(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        h_alice = {"Remote-User": "alice"}
        h_other = {"Remote-User": "alex"}

        # Save per-user provider, model, base_url, and API key for alice
        r_save = client.post(
            "/settings/llm",
            data={
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "llm_base_url": "https://openrouter.ai/api/v1/",
                "llm_api_key": "sk-user-secret-123",
            },
            headers=h_alice,
            follow_redirects=False,
        )
        assert r_save.status_code == 303

        r_settings = client.get("/settings", headers=h_alice)
        assert r_settings.status_code == 200
        assert "AI Configured (openai)" in r_settings.text
        assert "https://openrouter.ai/api/v1" in r_settings.text
        # Secret key value itself must never be leaked back in HTML
        assert "sk-user-secret-123" not in r_settings.text

        # Another user does not see alice's key
        r_other = client.get("/settings", headers=h_other)
        assert "No AI key saved" in r_other.text

        # Updating model with empty api_key keeps existing key
        r_upd = client.post(
            "/settings/llm",
            data={"llm_provider": "anthropic", "llm_model": "claude-3-5-haiku-latest", "llm_base_url": "", "llm_api_key": ""},
            headers=h_alice,
            follow_redirects=False,
        )
        assert r_upd.status_code == 303

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        row = conn.execute(
            "SELECT llm_provider, llm_model, llm_base_url, llm_api_key FROM users WHERE username = 'alice'"
        ).fetchone()
        conn.close()
        assert row == ("anthropic", "claude-3-5-haiku-latest", None, "sk-user-secret-123")

        # Clear saved key
        r_clr = client.post(
            "/settings/llm",
            data={"llm_provider": "gemini", "llm_model": "", "llm_base_url": "", "llm_api_key": "", "clear_api_key": "true"},
            headers=h_alice,
            follow_redirects=False,
        )
        assert r_clr.status_code == 303
        r_settings2 = client.get("/settings", headers=h_alice)
        assert "No AI key saved" in r_settings2.text


def test_llm_provider_presets_and_model_discovery(monkeypatch, tmp_path: Path):
    from app import main as main_module
    from app.services import LLM_PROVIDERS, extract_model_ids, resolve_llm_config

    # Mistral is a first-class provider with its own fixed endpoint.
    assert "mistral" in LLM_PROVIDERS
    cfg = resolve_llm_config({"llm_provider": "mistral", "llm_model": "mistral-large-latest", "llm_api_key": "k"})
    assert cfg["base_url"] == "https://api.mistral.ai/v1"
    assert cfg["provider"] == "mistral"

    # No provider has a default model any more.
    for provider in LLM_PROVIDERS:
        cfg = resolve_llm_config({"llm_provider": provider, "llm_model": "", "llm_api_key": "k"})
        assert cfg["model"] == "", provider
        assert cfg["has_model"] is False

    # The OpenAI-compatible base URL only applies to the openai provider.
    cfg = resolve_llm_config({"llm_provider": "mistral", "llm_base_url": "http://localhost:11434/v1"})
    assert cfg["base_url"] == "https://api.mistral.ai/v1"

    assert extract_model_ids("mistral", {
        "data": [
            {"id": "mistral-large-latest", "capabilities": {"completion_chat": True}},
            {"id": "mistral-embed", "capabilities": {"completion_chat": False}},
            {"id": "open-mistral-nemo", "capabilities": {"completion_chat": True}},
        ]
    }) == ["mistral-large-latest", "open-mistral-nemo"]
    assert extract_model_ids("openai", {
        "data": [{"id": "gpt-4o"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}]
    }) == ["gpt-4o"]
    assert extract_model_ids("gemini", {
        "models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        ]
    }) == ["gemini-2.5-flash"]

    async def fake_discover(cfg, timeout_s=20.0):
        assert cfg["provider"] == "mistral"
        return ["mistral-large-latest", "mistral-small-latest"]

    monkeypatch.setattr(main_module, "discover_models", fake_discover)
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}
        r = client.post(
            "/settings/llm/models",
            data={"llm_provider": "mistral", "llm_api_key": "test-key"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "provider": "mistral",
            "models": ["mistral-large-latest", "mistral-small-latest"],
        }


def test_ai_assist_requires_a_selected_model(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        headers = {"Remote-User": "alice"}
        r_save = client.post(
            "/settings/llm",
            data={"llm_provider": "mistral", "llm_model": "", "llm_base_url": "", "llm_api_key": "test-key"},
            headers=headers,
            follow_redirects=False,
        )
        assert r_save.status_code == 303

        r_settings = client.get("/settings", headers=headers)
        assert "Key saved · no model selected" in r_settings.text

        r_ai = client.post(
            "/months/2026-09/ai-assist",
            data={"mode": "ai"},
            headers={**headers, "X-Requested-With": "fetch"},
        )
        assert r_ai.status_code == 400
        assert "No AI model selected" in r_ai.json()["error"]

        r_test = client.post(
            "/settings/llm",
            data={"llm_provider": "mistral", "llm_model": "", "llm_base_url": "", "llm_api_key": "", "action_mode": "test"},
            headers=headers,
            follow_redirects=False,
        )
        assert r_test.status_code == 303
        assert "No%20AI%20model%20selected" in r_test.headers["location"]


def test_reasoning_setting_maps_to_provider_knobs(monkeypatch, tmp_path: Path):
    from app.services import (
        normalize_reasoning,
        reasoning_request_params,
        resolve_llm_config,
        should_retry_without_reasoning,
    )

    assert normalize_reasoning("HIGH") == "high"
    assert normalize_reasoning("max") == "off"
    assert normalize_reasoning(None) == "off"

    assert reasoning_request_params("mistral", "off") == {}
    assert reasoning_request_params("openai", "medium") == {"reasoning_effort": "medium"}
    anthropic = reasoning_request_params("anthropic", "low")
    assert anthropic["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert anthropic["max_tokens"] == 1024 + 2048
    assert reasoning_request_params("gemini", "high") == {"_gemini_thinking_budget": 4096}

    assert should_retry_without_reasoning(400, "reasoning_effort 'medium' is not supported")
    assert should_retry_without_reasoning(422, '{"detail": [{"loc": ["body", "thinking"]}]}')
    assert not should_retry_without_reasoning(400, "invalid response_format")
    assert not should_retry_without_reasoning(500, "reasoning exploded")

    cfg = resolve_llm_config({"llm_provider": "mistral", "llm_model": "m", "llm_reasoning": "high"})
    assert cfg["reasoning"] == "high"

    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        headers = {"Remote-User": "alice"}
        r_save = client.post(
            "/settings/llm",
            data={
                "llm_provider": "mistral",
                "llm_model": "mistral-large-latest",
                "llm_base_url": "",
                "llm_api_key": "test-key",
                "llm_reasoning": "high",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert r_save.status_code == 303

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        stored = conn.execute("SELECT llm_reasoning FROM users WHERE username = 'alice'").fetchone()[0]
        conn.close()
        assert stored == "high"

        page = client.get("/settings", headers=headers)
        assert '<option value="high" selected>' in page.text
        assert 'value="medium"' in page.text


def test_ai_assist_reports_reasoning_metadata(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=True) as client:
        headers = {"Remote-User": "alice", "X-Requested-With": "fetch"}
        r = client.post(
            "/months/2026-09/ai-assist",
            data={"instruction": "Review", "mode": "heuristic"},
            headers=headers,
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["reasoning"] == "off"
        assert payload["reasoning_tokens"] == 0
        assert payload["reasoning_note"] == ""


def test_ai_review_fixes_all_rule_violations_in_one_proposal(monkeypatch, tmp_path: Path):
    from app import services as services_module
    from demo_data import build_demo_month

    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        client.get("/", headers={"Remote-User": "alice"})
        db_file = tmp_path / "test_atoss.db"
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row

        bundle = build_demo_month("2026-10", seed=7, profile="messy")
        stamp = "2026-10-31T00:00:00+00:00"
        for entry in bundle["entries"]:
            conn.execute(
                """
                INSERT INTO entries (user_id, work_date, start_time, end_time, location,
                                     sources_json, client_event_id, deleted_at, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (entry["date"], entry["start"], entry["end"], entry["location"],
                 json.dumps([entry["source"]]), stamp, stamp),
            )
        conn.commit()

        user = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
        user.update({
            "llm_provider": "mistral",
            "llm_model": "test-model",
            "llm_api_key": "test-key",
            "llm_reasoning": "off",
        })

        month_data = services_module.get_month_data(conn, user, "2026-10")
        rule_changes = services_module._build_heuristic_ai_proposal(month_data, "")[2]
        unique_rule_keys = {(ch["date"], ch["op"]) for ch in rule_changes}
        assert len(unique_rule_keys) >= 5

        seen_payloads = []

        async def fake_call(cfg, system_prompt, user_content, timeout_s=90.0):
            seen_payloads.append(json.loads(user_content))
            return json.dumps({
                "summary": "Assistant reviewed the month.",
                "suspicious_days": [
                    {"date": "2026-10-21", "reason": "Short day", "suggested_check": "Check calendar"}
                ],
                "changes": [
                    {
                        "op": "set_day_code",
                        "date": "2026-10-30",
                        "day_code": "U",
                        "reason": "Assistant suggestion",
                    },
                    {
                        "op": next(iter(unique_rule_keys))[1],
                        "date": next(iter(unique_rule_keys))[0],
                        "before": [],
                        "after": [],
                        "reason": "duplicate of the rule scan",
                    },
                ],
            }), {"reasoning": "off", "reasoning_tokens": 0, "reasoning_note": ""}

        monkeypatch.setattr(services_module, "_call_llm_json", fake_call)
        proposal = asyncio.run(
            services_module.generate_inbuilt_ai_proposal(conn, user, "2026-10", instruction="")
        )

        # One review returns every rule fix plus what the model adds, and no duplicates.
        assert len(proposal["changes"]) == len(unique_rule_keys) + 1
        assert [c["source"] for c in proposal["changes"]].count("rule_scan") == len(unique_rule_keys)
        assert proposal["changes"][-1]["day_code"] == "U"
        assert len({(c["date"], c["op"]) for c in proposal["changes"]}) == len(proposal["changes"])
        assert "rule scan" in proposal["summary"]

        # The model is told what the rules already fixed, so it does not repeat them.
        assert len(seen_payloads[0]["rule_scan_changes"]) == len(rule_changes)
        conn.close()


def test_bundesbeamter_transition_and_13h_max(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}
        client.post(
            "/settings/profile",
            data={
                "display_name": "Alice",
                "timezone": "Europe/Berlin",
                "target_minutes": "492",
                "verbeamtung_date": "2026-09-23",
            },
            headers=headers,
            follow_redirects=False,
        )
        # Before Verbeamtung (2026-09-22): 10h30 worked exceeds 10:00 net cap
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-22", "start_time": "06:30", "end_time": "12:00", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-22", "start_time": "12:30", "end_time": "17:30", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )

        # From Verbeamtung onwards (2026-09-23): 10h30 net + 00:45 break = 11h15 presence is allowed (< 13:00)
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-23", "start_time": "06:30", "end_time": "12:00", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-23", "start_time": "12:45", "end_time": "17:45", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )

        # 2026-09-24: 12h30 net + 00:45 break = 13h15 presence exceeds 13:00 incl. breaks (§ 4 AZV)
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-24", "start_time": "06:15", "end_time": "12:00", "location": "office"},
            headers=headers,
            follow_redirects=False,
        )
        client.post(
            "/months/2026-09/entries",
            data={"work_date": "2026-09-24", "start_time": "12:45", "end_time": "19:30", "location": "office"},
            headers=headers,
            follow_redirects=False,
        )

        r_sep = client.get("/months/2026-09", headers=headers)
        assert r_sep.status_code == 200
        assert "07:48 (until 22.09.) · 08:12 (from 23.09.)" in r_sep.text
        assert "exceeding 10:00 daily max by 30m" in r_sep.text
        assert "exceeding 13:00 max (§ 4 AZV) by 15m" in r_sep.text
        # 2026-09-23 has 10:30 worked vs 08:12 Soll -> +02:18 delta and NO over_daily_max
        assert "+02:18" in r_sep.text
        assert "2026-09-23" in r_sep.text

        # Verify API payload details directly via agent token
        r_tok = client.post("/settings/tokens", data={"name": "test-azv", "scope": "agent"}, headers=headers)
        import re
        agent_token = re.search(r"atoss_a_[A-Za-z0-9_-]+", r_tok.text).group(0)
        r_agent = client.get("/api/agent/months/2026-09", headers={"Authorization": f"Bearer {agent_token}"})
        assert r_agent.status_code == 200
        issues = r_agent.json()["hard_issues"]
        over_max_dates = [it["date"] for it in issues if it["type"] == "over_daily_max"]
        assert over_max_dates == ["2026-09-22", "2026-09-24"]


def test_dashboard_today_editor_month_upload_and_delete_anchor(monkeypatch, tmp_path: Path):
    with _make_client(monkeypatch, tmp_path, seed_sep=False) as client:
        headers = {"Remote-User": "alice"}

        # Dashboard should show Stempeluhr + Today Editor, without Upload/Merge or Open Issues list
        r_dash = client.get("/", headers=headers)
        assert r_dash.status_code == 200
        assert "Live Stempeluhr" in r_dash.text
        assert "Today&#39;s Bookings" in r_dash.text or "Today's Bookings" in r_dash.text
        assert "Upload &amp; Merge ATOSS PDF" not in r_dash.text
        assert "Open Issues &amp; Suspicious Days" not in r_dash.text

        # Month Editor should contain Upload & Merge ATOSS PDF
        r_mon = client.get("/months/2026-10", headers=headers)
        assert r_mon.status_code == 200
        assert "Upload &amp; Merge ATOSS PDF" in r_mon.text

        # Create entry in Month Editor and delete -> redirects back to #day-YYYY-MM-DD
        client.post(
            "/months/2026-10/entries",
            data={"work_date": "2026-10-15", "start_time": "09:00", "end_time": "12:00", "location": "remote"},
            headers=headers,
            follow_redirects=False,
        )
        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        entry_id = conn.execute(
            "SELECT id FROM entries WHERE work_date = '2026-10-15' AND deleted_at IS NULL"
        ).fetchone()[0]
        conn.close()

        r_del_mon = client.post(
            f"/entries/{entry_id}/delete",
            data={"year_month": "2026-10"},
            headers=headers,
            follow_redirects=False,
        )
        assert r_del_mon.status_code == 303
        assert r_del_mon.headers["location"] == "/months/2026-10#day-2026-10-15"

        # Add/update/delete via Dashboard Today Editor -> redirects to redirect_to (/#today-editor)
        r_add_dash = client.post(
            "/months/2026-10/entries",
            data={
                "work_date": "2026-10-16",
                "start_time": "08:00",
                "end_time": "11:00",
                "location": "office",
                "redirect_to": "/#today-editor",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert r_add_dash.status_code == 303
        assert r_add_dash.headers["location"] == "/#today-editor"

        conn = sqlite3.connect(tmp_path / "test_atoss.db")
        entry2_id = conn.execute(
            "SELECT id FROM entries WHERE work_date = '2026-10-16' AND deleted_at IS NULL"
        ).fetchone()[0]
        conn.close()

        r_upd_dash = client.post(
            f"/entries/{entry2_id}/update",
            data={
                "year_month": "2026-10",
                "start_time": "08:15",
                "end_time": "11:30",
                "location": "remote",
                "redirect_to": "/#today-editor",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert r_upd_dash.status_code == 303
        assert r_upd_dash.headers["location"] == "/#today-editor"

        r_del_dash = client.post(
            f"/entries/{entry2_id}/delete",
            data={"year_month": "2026-10", "redirect_to": "/#today-editor"},
            headers=headers,
            follow_redirects=False,
        )
        assert r_del_dash.status_code == 303
        assert r_del_dash.headers["location"] == "/#today-editor"

        r_meta_dash = client.post(
            "/months/2026-10/days/2026-10-16",
            data={"day_code": "ZA", "note": "Zeitausgleich note", "redirect_to": "/#today-editor"},
            headers=headers,
            follow_redirects=False,
        )
        assert r_meta_dash.status_code == 303
        assert r_meta_dash.headers["location"] == "/#today-editor"

        # Add an interval for today's actual date and set today's note/code -> shows up on Dashboard
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today_iso = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
        today_ym = today_iso[:7]
        client.post(
            f"/months/{today_ym}/entries",
            data={
                "work_date": today_iso,
                "start_time": "09:15",
                "end_time": "10:45",
                "location": "remote",
                "redirect_to": "/#today-editor",
            },
            headers=headers,
            follow_redirects=False,
        )
        client.post(
            f"/months/{today_ym}/days/{today_iso}",
            data={"day_code": "ZA", "note": "Today status note", "redirect_to": "/#today-editor"},
            headers=headers,
            follow_redirects=False,
        )
        r_dash_after = client.get("/", headers=headers)
        assert "09:15 – 10:45" in r_dash_after.text
        assert "Today status note" in r_dash_after.text


def test_llm_list_content_normalization():
    from app.services import _coerce_llm_text, _parse_llm_json_dict

    assert _coerce_llm_text('{"summary": "ok"}') == '{"summary": "ok"}'
    assert _coerce_llm_text([{"thought": True, "text": "thinking"}, {"type": "text", "text": '{"ok": true}'}]) == '{"ok": true}'
    mistral_blocks = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "reasoning steps"}]},
        {"type": "text", "text": 'Here is the review:\n```json\n{"summary": "Mistral ok", "changes": []}\n```'},
    ]
    parsed = _parse_llm_json_dict(mistral_blocks, "openai", "magistral-medium-latest")
    assert parsed == {"summary": "Mistral ok", "changes": []}
    assert _coerce_llm_text(["line1", "line2"]) == "line1\nline2"
    assert _coerce_llm_text({"summary": "dict-json"}) == '{"summary": "dict-json"}'


def test_overall_balance_anchor_and_september_cap(tmp_path: Path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    headers = {"Remote-User": "alice"}
    with client:
        # Configure balance anchor on 2026-09-01 = +100:00 (will exceed +40:00 at end of Sep)
        r_save = client.post(
            "/settings/profile",
            data={
                "display_name": "Alice",
                "timezone": "Europe/Berlin",
                "target_minutes": "492",
                "verbeamtung_date": "2026-09-23",
                "balance_anchor_date": "2026-09-01",
                "balance_anchor_hm": "+200:00",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert r_save.status_code == 303

        # Mark all of October 2026 as U (0 Soll) except Oct 01 where we work 08:12 -> delta 00:00
        # Because Sep ended > +40:00, Oct must start capped at +40:00.
        r_sep = client.get("/months/2026-09", headers=headers)
        assert r_sep.status_code == 200
        assert "Total Balance" in r_sep.text
        assert "+40:00" in r_sep.text

        r_oct = client.get("/months/2026-10", headers=headers)
        assert r_oct.status_code == 200
        assert "Start +40:00" in r_oct.text

        r_dash = client.get("/", headers=headers)
        assert r_dash.status_code == 200
        assert "Total Balance" in r_dash.text







