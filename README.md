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
python cli.py merge atoss_aug.pdf Time-Recorder2.1_Timeproof_08.2025.pdf --issues August_2025_issues.csv
```

This parses both exports, merges and validates the entries, and writes `August_2025_review.txt` (month and year are inferred from the data).

After editing the report text file, re-validate it and rebuild the summary table:

```
python cli.py verify August_2025_review.txt --issues August_2025_verified_issues.csv
```

This produces the condensed `August_2025_report.txt` plus the optional issues CSV.

Notes:
- Location: maps token `ma` (case-insensitive, whole word) to `remote`; otherwise `office`.
- App PDF: defaults to `remote`; lines containing `Norm` switch to `office` and `Mobil` confirm `remote`.
- Times are detected per line with the date; multiple start/end pairs per day are supported.
