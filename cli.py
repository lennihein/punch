#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

from parser_atoss import parse_atoss_pdf
from parser_app import parse_app_pdf
from table_utils import parse_report_entries, render_monthly_report, render_reviewed_report
from validation import load_entries as load_entries_for_validation, merge_entries, validate, write_issues_csv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Parse PDFs into booked time entries (ATOSS)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_atoss = sub.add_parser("parse-atoss", help="Parse ATOSS work PDF into JSON list of intervals")
    p_atoss.add_argument("pdf", type=Path, help="Path to ATOSS PDF file")
    p_atoss.add_argument("-o", "--out", type=Path, help="Write JSON to this file")
    p_atoss.add_argument("--pretty", action="store_true", help="Pretty-print JSON")

    p_app = sub.add_parser("parse-app", help="Parse Timerecordlist app PDF into JSON list of intervals")
    p_app.add_argument("pdf", type=Path, help="Path to App PDF file")
    p_app.add_argument("-o", "--out", type=Path, help="Write JSON to this file")
    p_app.add_argument("--pretty", action="store_true", help="Pretty-print JSON")

    p_val = sub.add_parser("validate", help="Merge entries and validate rules")
    p_val.add_argument("files", nargs="+", type=Path, help="JSON files with entries list (app + work)")
    p_val.add_argument("-o", "--out", type=Path, help="Write merged+validated JSON to this file")
    p_val.add_argument("--issues", type=Path, help="Write issues to CSV")
    p_val.add_argument("--pretty", action="store_true", help="Pretty-print JSON")

    p_tab = sub.add_parser("table", help="Render merged entries into a daily report")
    p_tab.add_argument("files", nargs="+", type=Path, help="One or more JSON files with entries list")
    p_tab.add_argument("-o", "--out", type=Path, help="Write output to this file")

    p_verify = sub.add_parser("verify-report", help="Validate and re-render a manually edited report")
    p_verify.add_argument("report", type=Path, help="Path to edited report text file")
    p_verify.add_argument("-o", "--out", type=Path, help="Write reviewed report to this file")
    p_verify.add_argument("--issues", type=Path, help="Write issues to CSV")

    args = parser.parse_args(argv)

    if args.cmd == "parse-atoss":
        entries = parse_atoss_pdf(args.pdf)
        js = json.dumps(entries, indent=2 if args.pretty else None, ensure_ascii=False)
        if args.out:
            args.out.write_text(js, encoding="utf-8")
        else:
            print(js)
        return 0

    if args.cmd == "parse-app":
        entries = parse_app_pdf(args.pdf)
        js = json.dumps(entries, indent=2 if args.pretty else None, ensure_ascii=False)
        if args.out:
            args.out.write_text(js, encoding="utf-8")
        else:
            print(js)
        return 0

    if args.cmd == "validate":
        all_entries = load_entries_for_validation(args.files)
        merged = merge_entries(all_entries)
        result = validate(merged)
        js = json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False)
        if args.out:
            args.out.write_text(js, encoding="utf-8")
        else:
            print(js)
        if args.issues:
            write_issues_csv(result.get("issues", []), args.issues)
        return 0

    if args.cmd == "table":
        out = render_monthly_report(args.files)
        if args.out:
            args.out.write_text(out, encoding="utf-8")
        else:
            print(out)
        return 0

    if args.cmd == "verify-report":
        manual_entries = parse_report_entries(args.report)
        if not manual_entries:
            print(f"No entries found in {args.report}", file=sys.stderr)
            return 1
        merged_manual = merge_entries(manual_entries)
        result = validate(merged_manual)
        report_text = render_reviewed_report(result)
        if args.out:
            args.out.write_text(report_text, encoding="utf-8")
        else:
            print(report_text)
        if args.issues:
            write_issues_csv(result.get("issues", []), args.issues)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
