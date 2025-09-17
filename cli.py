#!/usr/bin/env python3
import argparse
import sys
from datetime import datetime, date as _date
from pathlib import Path
from typing import Iterable

from parser_atoss import parse_atoss_pdf
from parser_app import parse_app_pdf
from table_utils import (
    find_empty_workdays,
    parse_report_entries,
    render_review_report,
    render_reviewed_report,
    summarize_issues,
)
from validation import merge_entries, validate, write_issues_csv


def _slug_from_dates(dates: Iterable[str]) -> str | None:
    for date_s in sorted(d for d in dates if d):
        try:
            dt = datetime.strptime(date_s, "%Y-%m-%d")
        except Exception:
            continue
        return dt.strftime("%B_%y").lower()
    return None


def _month_slug(result: dict) -> str:
    entries = result.get("entries", []) or []
    slug = _slug_from_dates(e.get("date") for e in entries if isinstance(e, dict))
    if slug:
        return slug
    days = result.get("days", []) or []
    slug = _slug_from_dates(d.get("date") for d in days if isinstance(d, dict))
    if slug:
        return slug
    return _date.today().strftime("%B_%y").lower()


def run_merge(args: argparse.Namespace) -> int:
    atoss_entries = parse_atoss_pdf(args.atoss_pdf)
    app_entries = parse_app_pdf(args.app_pdf)

    merged_entries = merge_entries(list(atoss_entries) + list(app_entries))
    result = validate(merged_entries)
    report_text = render_review_report(result)

    month_slug = _month_slug(result)
    out_path = args.out if args.out else Path(f"{month_slug}.txt")
    out_path.write_text(report_text, encoding="utf-8")
    print(f"Wrote review report to {out_path}")

    issue_lines = summarize_issues(result.get("issues", []) or [])
    if issue_lines:
        print("Issues summary:")
        for line in issue_lines:
            print(f"  {line}")
    else:
        print("Issues summary: none")

    empty_days = find_empty_workdays(merged_entries, result.get("days", []) or [])
    if empty_days:
        print("Empty workdays without entries:")
        for label in empty_days:
            print(f"  {label}")
    else:
        print("Empty workdays without entries: none")

    if args.issues:
        write_issues_csv(result.get("issues", []), args.issues)
        print(f"Wrote issues CSV to {args.issues}")

    return 0


def run_verify(args: argparse.Namespace) -> int:
    manual_entries = parse_report_entries(args.report)
    if not manual_entries:
        print(f"No entries found in {args.report}", file=sys.stderr)
        return 1

    merged_manual = merge_entries(manual_entries)
    result = validate(merged_manual)
    report_text = render_reviewed_report(result)

    month_slug = _month_slug(result)
    out_path = args.out if args.out else Path(f"{month_slug}.txt")
    out_path.write_text(report_text, encoding="utf-8")
    print(f"Wrote verified report to {out_path}")

    issue_lines = summarize_issues(result.get("issues", []) or [])
    if issue_lines:
        print("Issues summary:")
        for line in issue_lines:
            print(f"  {line}")
    else:
        print("Issues summary: none")

    empty_days = find_empty_workdays(merged_manual, result.get("days", []) or [])
    if empty_days:
        print("Empty workdays without entries:")
        for label in empty_days:
            print(f"  {label}")
    else:
        print("Empty workdays without entries: none")

    if args.issues:
        write_issues_csv(result.get("issues", []), args.issues)
        print(f"Wrote issues CSV to {args.issues}")

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ATOSS/App PDF merge + review reports")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="Parse PDFs, merge entries, and produce a review report")
    p_merge.add_argument("atoss_pdf", type=Path, help="ATOSS work PDF export")
    p_merge.add_argument("app_pdf", type=Path, help="Timerecordlist app PDF export")
    p_merge.add_argument("--issues", type=Path, help="Optional path to write issues CSV")
    p_merge.add_argument("-o", "--out", type=Path, help="Write review report to this file (defaults to <month>_<yy>.txt)")
    p_merge.set_defaults(func=run_merge)

    p_verify = sub.add_parser("verify", help="Validate an edited report and rebuild the summary")
    p_verify.add_argument("report", type=Path, help="Edited review report text file")
    p_verify.add_argument("--issues", type=Path, help="Optional path to write issues CSV")
    p_verify.add_argument("-o", "--out", type=Path, help="Write verified report to this file (defaults to <month>_<yy>.txt)")
    p_verify.set_defaults(func=run_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
