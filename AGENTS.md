# Punch! — Agent Instructions

**Punch!** is a time-tracking workspace, FastAPI webapp, CLI report tool, and Wear OS client designed around German public-sector / ATOSS time-tracking workflows.

## Architecture & Repository Layout

- **`cli.py`**: CLI entry points (`merge`, `verify`, `pdf`) for parsing ATOSS PDF / legacy app exports, running compliance validation, and compiling clean printable A4 PDF overviews via Typst.
- **`parser_atoss.py` & `parser_app.py`**: Extract work intervals and absence codes (`K`, `S`, `U`, `F`, `ZA`, `D`, `D1`, `D2`) from monthly ATOSS PDFs and legacy app PDFs.
- **`validation.py`**: Deterministic rule validation:
  - Up through `2026-09-22` (Tarif): daily Soll `07:48` (`468` min), max net work `10:00` (`600` min).
  - From `2026-09-23` (`VERBEAMTUNG_DATE`, Bundesbeamter / AZV): daily Soll `08:12` (`492` min), max presence incl. breaks `13:00` (`780` min).
  - Break minimums: `> 06:30` work requires `30m` break; `> 09:45` work requires `45m` break.
  - Rahmenarbeitszeit: `06:00–20:00` Mon–Thu, `06:00–18:00` Fri.
- **`table_utils.py`**: Data normalization, Typst document generation (`build_month_pdf_data`, `compile_typst_pdf`), and formatting rules (see `.agents/rules/atoss.md`).
- **`app/`**: FastAPI + Jinja2 + SQLite WAL web application (`main.py`, `db.py`, `auth.py`, `services.py`, `static/style.css`, `templates/`).
  - Protected in production via Authelia `forward_auth` (`Remote-User` / `Remote-Email`) except `/api/watch/*`, `/api/agent/*`, and `/api/health`.
  - Per-user LLM configuration (`gemini`, OpenAI-compatible, `anthropic`) for AI month review proposals.
- **`wear/`**: Kotlin + Jetpack Compose for Wear OS app (`WearApp.kt`, `MainActivity.kt`) & Wear OS Tile (`PunchTileService.kt`) with offline punch queue (`PendingPunch` + WorkManager `PunchSyncWorker`).
- **`flake.nix`**: Nix dev shells (`default` Python + Typst + Inter; `wear` Android SDK 35 + Gradle + ADB), `punch-web` package, and `nixosModules.default` (`services.punch`).

## Development Commands

- **Run webapp tests:**
  ```bash
  nix develop --command pytest -v tests/test_webapp.py
  ```
- **Run CLI tool:**
  ```bash
  nix develop --command python cli.py verify <report.txt>
  nix develop --command python cli.py pdf <report.txt>
  ```
- **Build Wear OS APK:**
  ```bash
  nix develop .#wear --impure --command bash -c "cd wear && gradle assembleDebug --no-daemon"
  ```

## Important Rules

- Follow PDF export formatting rules in `.agents/rules/atoss.md`.
- **Never commit or push personal data:** `.pdf`, `*_26.txt`, `export_*.txt`, `*.db*`, or `.agents/rules/local.md` must stay untracked/ignored.
- Keep local-only hostnames, IPs, watch ADB targets, and private deployment habits in `.agents/rules/local.md` (gitignored), not in tracked files.
