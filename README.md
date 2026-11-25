# Punch!

Time-tracking assistant, FastAPI webapp, CLI PDF parser/exporter, and Wear OS client tailored for German public-sector / ATOSS workflows.

## Components

- **Webapp (`app/`)**: Live Stempeluhr dashboard, monthly interval & day-code editor, ATOSS PDF upload/merge, per-user AI month review assistant (Gemini, Mistral, OpenAI-compatible, Anthropic — model chosen explicitly, discoverable from the provider; every review runs the deterministic rule scan first, so one proposal covers all hard violations and the model only adds what rules miss), Typst A4 PDF export, and Watch/Agent REST APIs.
- **Wear OS Client (`wear/`)**: Kotlin + Jetpack Compose watch app and quick-action Tile with offline punch queueing (`WorkManager`). See [wear/README.md](wear/README.md).
- **CLI (`cli.py`)**: Merge, validate, and export ATOSS & legacy Time-Recorder exports directly from the terminal.

## Install & Dev Shell

- **Nix + direnv (recommended):**
  - `direnv allow` (or `nix develop`) enters a shell with Python, FastAPI, Typst, and Inter fonts.
  - `nix develop .#wear --impure` provides JDK 17, Gradle, Android SDK 35, and `adb`.
- **Plain Python:** Python 3.10+ with dependencies in `flake.nix` (`fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `pdfminer-six`, `httpx`, `pytest`) + `typst`.

## CLI Usage

Create a fresh review report directly from two PDF exports:

```bash
python cli.py merge atoss_aug.pdf Time-Recorder2.1_Timeproof_08.2025.pdf --issues august_25_issues.csv
```

Re-validate after editing a report text file and rebuild the summary table:

```bash
python cli.py verify august_25.txt --issues august_25_verified_issues.csv
```

Pass `-o/--out` to either command to override the default `<month>_<yy>.txt` path.

### Generate Synthetic Test Data

The AI assistant refuses to invent worked hours, so test months come from a seeded generator instead:

```bash
# 2026-11 with a realistic mix of break, Rahmenzeit and overlap violations
python cli.py demo --month 2026-11

# A clean month, or a deliberately broken one, reproducible via --seed
python cli.py demo --month 2026-11 --profile clean --seed 3
```

The output is a normal review report (`demo_<month>_<yy>.txt`): upload it in the webapp, or point `SEED_REPORT_PATHS` at it. `--profile` picks `mixed`, `clean` or `messy`; `--until-today` stops at the current date instead of filling the whole month.

### Generate Printable PDF Overview
Generate an ink-saving, clean printable PDF overview (for ATOSS manual entry):

```bash
# Single month overview
python cli.py pdf export_may_26.txt

# Multiple months compiled into one PDF
python cli.py pdf export_may_26.txt export_june_26.txt export_july_26.txt -o export_may_june_july_2026.pdf
```

Notes:
- Location: maps token `ma` (case-insensitive, whole word) to `remote`; otherwise `office`.
- App PDF: defaults to `remote`; lines containing `Norm` switch to `office` and `Mobil` confirms `remote`.
- Times are detected per line with the date; multiple start/end pairs per day are supported.
- Weekdays without entries are flagged so holidays or absences (`U`, `ZA`, `K`, `F`, `D`, `S`) can be recorded explicitly.
