# PDF Time Parser (ATOSS)

## Install

- Option A: Nix + direnv (recommended)
  - Install `nix` and `direnv`, then in this repo:
    - `direnv allow` (or `nix develop` without direnv)
    - A Python venv `.venv` is created and `requirements.txt` installed automatically.

- Option B: Plain Python
  - Python 3.10+
  - `pip install -r requirements.txt`

## Usage

Create a fresh review report directly from the two PDFs:

```
python cli.py merge atoss_aug.pdf Time-Recorder2.1_Timeproof_08.2025.pdf --issues august_25_issues.csv
```

This parses both exports, merges and validates the entries, and writes `august_25.txt` (month and year inferred as `<month>_<yy>` in lowercase).

After editing the report text file, re-validate it and rebuild the summary table:

```
python cli.py verify august_25.txt --issues august_25_verified_issues.csv
```

This overwrites `august_25.txt` with the condensed verified view and optionally writes an updated issues CSV. Re-run `merge` if you ever need to regenerate the editable version.

Pass `-o/--out` to either command if you want to override the default `<month>_<yy>.txt` location.

Notes:
- Location: maps token `ma` (case-insensitive, whole word) to `remote`; otherwise `office`.
- App PDF: defaults to `remote`; lines containing `Norm` switch to `office` and `Mobil` confirm `remote`.
- Times are detected per line with the date; multiple start/end pairs per day are supported.
- Reports include an issue summary ahead of the daily sections for quick scanning.
- Weekdays in the month without any entries are listed so you can fill in holidays or missed logs explicitly.
