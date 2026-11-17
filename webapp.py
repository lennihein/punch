#!/usr/bin/env python3
"""Single-user web UI around the ATOSS merge/verify/pdf pipeline.

Binds to loopback only and requires a Remote-User header, so it must sit
behind a reverse proxy with Authelia forward auth (see README).
"""
import base64
import contextlib
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cli
from table_utils import parse_report_entries

DATA_DIR = Path(os.environ.get("ATOSS_DATA_DIR", "./data")).resolve()
HOST = os.environ.get("ATOSS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ATOSS_PORT", "8088"))
MAX_BODY = int(os.environ.get("ATOSS_MAX_BODY", str(25 * 1024 * 1024)))
REPORT_NAME_RE = re.compile(r"^[a-z0-9_]+\.txt$")

_CLI_LOCK = threading.Lock()

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>atoss</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #111; color: #eee; }
  main { max-width: 860px; margin: 0 auto; padding: 12px; }
  h1 { font-size: 20px; }
  textarea { width: 100%; min-height: 45vh; box-sizing: border-box; font-family: ui-monospace, monospace;
             font-size: 13px; background: #1b1b1b; color: #eee; border: 1px solid #333; border-radius: 6px; padding: 8px; }
  button { font-size: 15px; padding: 8px 14px; margin: 4px 4px 4px 0; border-radius: 6px;
           border: 1px solid #444; background: #2a2a2a; color: #eee; }
  button.primary { background: #2563eb; border-color: #2563eb; }
  pre { white-space: pre-wrap; background: #1b1b1b; border: 1px solid #333; border-radius: 6px;
        padding: 8px; max-height: 30vh; overflow: auto; font-size: 12px; }
  select, input[type=file] { color: #eee; background: #1b1b1b; border: 1px solid #333; border-radius: 6px; padding: 6px; max-width: 100%; }
  .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0; }
  label { font-size: 13px; color: #aaa; }
</style>
</head>
<body>
<main>
  <h1>ATOSS merge</h1>
  <div class="row">
    <label>ATOSS PDF <input type="file" id="atoss" accept="application/pdf"></label>
    <label>App PDF (optional) <input type="file" id="app" accept="application/pdf"></label>
  </div>
  <div class="row">
    <select id="saved"></select>
    <button id="load">Load</button>
    <button id="merge" class="primary">Merge</button>
    <button id="verify">Verify</button>
    <button id="txt">Download TXT</button>
    <button id="pdf">Download PDF</button>
  </div>
  <textarea id="report" spellcheck="false"></textarea>
  <pre id="log"></pre>
</main>
<script>
const $ = id => document.getElementById(id);
const log = m => { $("log").textContent = m; };
function readB64(file) {
  if (!file) return Promise.resolve(null);
  return new Promise(res => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.readAsDataURL(file);
  });
}
async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}
$("merge").onclick = async () => {
  try {
    log("merging...");
    const atoss = await readB64($("atoss").files[0]);
    if (!atoss) throw new Error("choose the ATOSS PDF first");
    const app = await readB64($("app").files[0]);
    const data = await post("/api/merge", { atoss_pdf: atoss, app_pdf: app });
    $("report").value = data.report;
    log((data.log || "ok").trim() + (data.saved ? "\\nsaved as " + data.saved : ""));
  } catch (e) { log("error: " + e.message); }
};
$("verify").onclick = async () => {
  try {
    log("verifying...");
    const data = await post("/api/verify", { report: $("report").value });
    $("report").value = data.report;
    log((data.log || "ok").trim() + (data.saved ? "\\nsaved as " + data.saved : ""));
  } catch (e) { log("error: " + e.message); }
};
$("pdf").onclick = async () => {
  try {
    log("rendering pdf...");
    const data = await post("/api/pdf", { report: $("report").value });
    const a = document.createElement("a");
    a.href = "data:application/pdf;base64," + data.pdf;
    a.download = "atoss.pdf";
    a.click();
    log("pdf downloaded");
  } catch (e) { log("error: " + e.message); }
};
$("txt").onclick = () => {
  const a = document.createElement("a");
  a.href = "data:text/plain;charset=utf-8," + encodeURIComponent($("report").value);
  a.download = "report.txt";
  a.click();
};
$("load").onclick = async () => {
  const name = $("saved").value;
  if (!name) return;
  const res = await fetch("/api/report?name=" + encodeURIComponent(name));
  if (!res.ok) { log("error: " + res.statusText); return; }
  $("report").value = await res.text();
};
async function refresh() {
  const res = await fetch("/api/state");
  const data = await res.json();
  if (data.report) $("report").value = data.report;
  $("saved").innerHTML = (data.reports || []).map(r => "<option>" + r + "</option>").join("");
}
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "atoss-web/1.0"

    def log_message(self, fmt, *args):
        pass

    def _user(self):
        user = self.headers.get("Remote-User") or self.headers.get("X-Forwarded-User")
        if not user:
            return None
        user = re.sub(r"[^A-Za-z0-9_.-]", "_", user)
        if not user or ".." in user:
            return None
        return user

    def _require_user(self):
        user = self._user()
        if not user:
            self._send_json({"ok": False, "error": "unauthorized: missing Remote-User header (must sit behind Authelia forward auth)"}, 401)
            return None
        return user

    def _paths(self, user):
        work = DATA_DIR / user / "work"
        reports = DATA_DIR / user / "reports"
        for d in (work, reports):
            d.mkdir(parents=True, exist_ok=True)
        return work, reports

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8") or "{}")

    def _run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def _save_copy(self, report_path, reports_dir):
        data = parse_report_entries(report_path)
        slug = cli._month_slug({"entries": data.get("entries", []), "days": []})
        if slug:
            (reports_dir / f"{slug}.txt").write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
        return slug

    def _handle_state(self, user):
        work, reports = self._paths(user)
        current = work / "current.txt"
        report = current.read_text(encoding="utf-8") if current.exists() else ""
        names = sorted(p.name for p in reports.glob("*.txt"))
        self._send_json({"ok": True, "user": user, "report": report, "reports": names})

    def _handle_merge(self, user):
        payload = self._read_json()
        atoss_b64 = payload.get("atoss_pdf")
        if not atoss_b64:
            self._send_json({"ok": False, "error": "atoss_pdf (base64) is required"}, 400)
            return
        work, reports = self._paths(user)
        atoss_path = work / "atoss.pdf"
        atoss_path.write_bytes(base64.b64decode(atoss_b64))
        argv = ["merge", str(atoss_path)]
        app_b64 = payload.get("app_pdf")
        if app_b64:
            app_path = work / "app.pdf"
            app_path.write_bytes(base64.b64decode(app_b64))
            argv.append(str(app_path))
        current = work / "current.txt"
        argv += ["-o", str(current)]
        with _CLI_LOCK:
            rc, log = self._run_cli(argv)
        if rc != 0 or not current.exists():
            self._send_json({"ok": False, "error": log.strip() or f"merge failed (rc={rc})"}, 500)
            return
        report = current.read_text(encoding="utf-8")
        saved = self._save_copy(current, reports)
        self._send_json({"ok": True, "report": report, "log": log, "saved": saved})

    def _handle_verify(self, user):
        payload = self._read_json()
        report = payload.get("report")
        if not report or not report.strip():
            self._send_json({"ok": False, "error": "report text is required"}, 400)
            return
        work, reports = self._paths(user)
        current = work / "current.txt"
        current.write_text(report, encoding="utf-8")
        with _CLI_LOCK:
            rc, log = self._run_cli(["verify", str(current), "-o", str(current)])
        if rc != 0:
            self._send_json({"ok": False, "error": log.strip() or f"verify failed (rc={rc})"}, 500)
            return
        new_report = current.read_text(encoding="utf-8")
        saved = self._save_copy(current, reports)
        self._send_json({"ok": True, "report": new_report, "log": log, "saved": saved})

    def _handle_pdf(self, user):
        payload = self._read_json()
        report = payload.get("report")
        work, _reports = self._paths(user)
        current = work / "current.txt"
        if report and report.strip():
            current.write_text(report, encoding="utf-8")
        if not current.exists():
            self._send_json({"ok": False, "error": "no report yet - merge or verify first"}, 400)
            return
        pdf_path = work / "current.pdf"
        with _CLI_LOCK:
            rc, log = self._run_cli(["pdf", str(current), "-o", str(pdf_path)])
        if rc != 0 or not pdf_path.exists():
            self._send_json({"ok": False, "error": log.strip() or f"pdf generation failed (rc={rc})"}, 500)
            return
        self._send_json({"ok": True, "pdf": base64.b64encode(pdf_path.read_bytes()).decode("ascii")})

    def _handle_report_file(self, user, query):
        name = parse_qs(query).get("name", [""])[0]
        if not REPORT_NAME_RE.match(name):
            self._send_json({"ok": False, "error": "bad report name"}, 400)
            return
        path = DATA_DIR / user / "reports" / name
        if not path.exists():
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        self._send_text(path.read_text(encoding="utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(INDEX_HTML, ctype="text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/state":
            user = self._require_user()
            if user:
                self._handle_state(user)
            return
        if parsed.path == "/api/report":
            user = self._require_user()
            if user:
                self._handle_report_file(user, parsed.query)
            return
        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        user = self._require_user()
        if not user:
            return
        try:
            if parsed.path == "/api/merge":
                self._handle_merge(user)
            elif parsed.path == "/api/verify":
                self._handle_verify(user)
            elif parsed.path == "/api/pdf":
                self._handle_pdf(user)
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"atoss-web listening on http://{HOST}:{PORT} (data dir: {DATA_DIR})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
