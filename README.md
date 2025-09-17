# PDF Time Parser (ATOSS)

This tool parses ATOSS work PDFs into a JSON list of booked time intervals. It focuses on extracting per-day intervals and location (office vs remote) where ATOSS marks remote as "ma".

Status: MVP parser for ATOSS PDFs. Validation/merging/transfers follow later.

## Install

- Option A: Nix + direnv (recommended)
  - Install `nix` and `direnv`, then in this repo:
    - `direnv allow` (or `nix develop` without direnv)
    - A Python venv `.venv` is created and `requirements.txt` installed automatically.

- Option B: Plain Python
  - Python 3.10+
  - `pip install -r requirements.txt`

## Usage

Parse an ATOSS PDF into JSON (printed to stdout):

```
python cli.py parse-atoss Time-Recorder2.1_Timeproof_08.2025.pdf --pretty
```

Write to a file:

```
python cli.py parse-atoss input.pdf -o entries.json
```

## Output

A list of intervals like:

```
[
  {
    "source": "work",
    "date": "2025-08-01",
    "start": "08:00",
    "end": "16:30",
    "minutes": 510,
    "location": "remote"  // when ATOSS shows "ma" on that line/day
  }
]
```

Notes:
- Location: maps token `ma` (case-insensitive, whole word) to `remote`; otherwise `office`.
- App PDF: defaults to `remote`; lines containing `Norm` switch to `office` and `Mobil` confirm `remote`.
- Times are detected per line with the date; multiple start/end pairs per day are supported.
- This MVP relies on text extraction and robust regexes; for edge layouts we can add per-version tweaks once we see more samples.

## Limitations

- PDF text extraction can vary; if your file layout differs, please share a sample so we can adapt matchers.
- Currently supports ATOSS PDFs. We can add your app’s PDF parser next.

## Dev

Parse your timetracker app PDF ("Timerecordlist"):

```
python cli.py parse-app Time-Recorder2.1_Timeproof_08.2025.pdf --pretty -o app_entries.json
```

### Merge + Validate

Create a merged set with issues:

```
python cli.py validate app_entries.json atoss_entries.json --pretty -o merged.json --issues issues.csv
```

Render a daily report from the merged entries (includes per-day totals and issues):

```
python cli.py table merged.json -o merged_report.txt
```

After you adjust `merged_report.txt` manually, revalidate it and rebuild the summary:

```
python cli.py verify-report merged_report.txt --issues reviewed_issues.csv -o reviewed_report.txt
```

### Full Workflow

1. Parse ATOSS: `python cli.py parse-atoss atoss_aug.pdf --pretty -o atoss_entries.json`
2. Parse app: `python cli.py parse-app Time-Recorder2.1_Timeproof_08.2025.pdf --pretty -o app_entries.json`
3. Validate & merge: `python cli.py validate app_entries.json atoss_entries.json --pretty -o merged.json --issues issues.csv`
4. Build report: `python cli.py table merged.json -o merged_report.txt`
5. Edit `merged_report.txt`, then re-validate: `python cli.py verify-report merged_report.txt --issues reviewed_issues.csv -o reviewed_report.txt`

