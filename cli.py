#!/usr/bin/env python3
import argparse
import sys
from datetime import datetime, date as _date
from pathlib import Path
from typing import Iterable

from demo_data import build_demo_month, demo_filename, normalize_month
from parser_atoss import parse_atoss_pdf
from parser_app import parse_app_pdf
from table_utils import (
    export_pdf_from_reports,
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
    atoss_data = parse_atoss_pdf(args.atoss_pdf)
    atoss_entries = atoss_data.get("entries", [])
    atoss_day_codes = atoss_data.get("day_codes", {})
    app_entries = parse_app_pdf(args.app_pdf)

    merged_entries = merge_entries(list(atoss_entries) + list(app_entries), extra_day_codes=atoss_day_codes)
    result = validate(merged_entries, extra_day_codes=atoss_day_codes)
    month_slug = _month_slug(result)
    out_path = args.out if args.out else Path(f"{month_slug}.txt")

    day_notes = {}
    combined_day_codes = atoss_day_codes.copy()

    if out_path.exists():
        print(f"Existing report {out_path} found. Merging current edits...")
        report_data = parse_report_entries(out_path)
        existing_entries = report_data.get("entries", [])
        day_notes = report_data.get("day_notes", {})
        existing_day_codes = report_data.get("day_codes", {})
        
        # Merge day codes: existing (manual edits) take precedence
        for d, c in existing_day_codes.items():
            combined_day_codes[d] = c

        if existing_entries:
            # Re-merge everything including existing
            merged_entries = merge_entries(list(atoss_entries) + list(app_entries) + list(existing_entries), extra_day_codes=combined_day_codes)
            result = validate(merged_entries, extra_day_codes=combined_day_codes)
    
    report_text = render_review_report(result, day_notes=day_notes, day_codes=combined_day_codes)

    out_path.write_text(report_text, encoding="utf-8")
    print(f"Wrote review report to {out_path}")

    issue_lines = summarize_issues(result.get("issues", []) or [])
    if issue_lines:
        print("Issues summary:")
        for line in issue_lines:
            print(f"  {line}")
    else:
        print("Issues summary: none")

    empty_days = find_empty_workdays(merged_entries, result.get("days", []) or [], day_codes=combined_day_codes)
    if empty_days:
        print("Empty workdays without entries:")
        for label in empty_days:
            # Add code suffix for consistency in terminal
            dt_part = label.split(" ")[0]
            code = combined_day_codes.get(dt_part)
            suffix = f" [{code}]" if code and code != "office" else ""
            print(f"  {label}{suffix}")
    else:
        print("Empty workdays without entries: none")

    if args.issues:
        write_issues_csv(result.get("issues", []), args.issues)
        print(f"Wrote issues CSV to {args.issues}")

    return 0


def run_verify(args: argparse.Namespace) -> int:
    report_data = parse_report_entries(args.report)
    manual_entries = report_data.get("entries", [])
    day_notes = report_data.get("day_notes", {})
    day_codes = report_data.get("day_codes", {})
    if not manual_entries and not day_notes and not day_codes:
        print(f"No entries found in {args.report}", file=sys.stderr)
        return 1

    merged_manual = merge_entries(manual_entries, extra_day_codes=day_codes)
    result = validate(merged_manual, extra_day_codes=day_codes)
    report_text = render_reviewed_report(result, day_notes=day_notes, day_codes=day_codes)

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

    empty_days = find_empty_workdays(merged_manual, result.get("days", []) or [], day_codes=day_codes)
    if empty_days:
        print("Empty workdays without entries:")
        for label in empty_days:
            dt_part = label.split(" ")[0]
            code = day_codes.get(dt_part)
            suffix = f" [{code}]" if code and code != "office" else ""
            print(f"  {label}{suffix}")
    else:
        print("Empty workdays without entries: none")

    if args.issues:
        write_issues_csv(result.get("issues", []), args.issues)
        print(f"Wrote issues CSV to {args.issues}")

    return 0


def run_pdf(args: argparse.Namespace) -> int:
    reports: List[Path] = args.reports
    if not reports:
        print("Error: No report files specified.", file=sys.stderr)
        return 1

    for r in reports:
        if not r.exists():
            print(f"Error: Report file {r} does not exist.", file=sys.stderr)
            return 1

    if args.out:
        out_path = args.out
    elif len(reports) == 1:
        out_path = reports[0].with_suffix(".pdf")
    else:
        # Construct merged name from stem names
        stems = [r.stem for r in reports]
        out_path = Path(f"{'_'.join(stems)}.pdf")

    try:
        export_pdf_from_reports(reports, out_path)
        print(f"Wrote PDF overview to {out_path}")
        return 0
    except Exception as e:
        print(f"Error generating PDF: {e}", file=sys.stderr)
        return 1


def run_demo(args: argparse.Namespace) -> int:
    year_month = normalize_month(args.month)
    if not year_month:
        print(f"Invalid --month '{args.month}', expected e.g. 2026-11", file=sys.stderr)
        return 2

    bundle = build_demo_month(
        year_month,
        seed=args.seed,
        profile=args.profile,
        through_today=args.until_today,
    )
    merged = merge_entries(bundle["entries"], extra_day_codes=bundle["day_codes"])
    result = validate(merged, extra_day_codes=bundle["day_codes"])
    report_text = render_reviewed_report(result, day_notes=bundle["day_notes"], day_codes=bundle["day_codes"])

    out_path = args.out if args.out else Path(demo_filename(year_month))
    out_path.write_text(report_text, encoding="utf-8")

    days = len({e["date"] for e in bundle["entries"]})
    issues = result.get("issues", []) or []
    print(f"Wrote synthetic report to {out_path}")
    print(f"  {len(bundle['entries'])} intervals on {days} days · {len(bundle['day_codes'])} day codes · {len(issues)} issues")
    for line in summarize_issues(issues) or ["none"]:
        print(f"  - {line}")
    print("Import it via Upload in the webapp, or point SEED_REPORT_PATHS at it.")
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

    p_pdf = sub.add_parser("pdf", help="Generate an ink-saving printable PDF overview from review report text files")
    p_pdf.add_argument("reports", type=Path, nargs="+", help="One or more review report text files (e.g. export_may_26.txt)")
    p_pdf.add_argument("-o", "--out", type=Path, help="Output PDF path (defaults to <month>_<yy>.pdf or combined name)")
    p_pdf.set_defaults(func=run_pdf)

    p_demo = sub.add_parser("demo", help="Generate a synthetic month report for demos and AI-assist testing")
    p_demo.add_argument("--month", required=True, help="Year-month to generate, e.g. 2026-11")
    p_demo.add_argument("--seed", type=int, default=7, help="Random seed; the same seed reproduces the month")
    p_demo.add_argument("--profile", choices=["mixed", "clean", "messy"], default="mixed", help="How many rule violations to bake in")
    p_demo.add_argument("--until-today", action="store_true", help="Stop at today instead of filling the whole month")
    p_demo.add_argument("-o", "--out", type=Path, help="Output report path (defaults to demo_<month>_<yy>.txt)")
    p_demo.set_defaults(func=run_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
