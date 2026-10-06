"""Developer-only corpus commands; no financial contents or paths in output."""

import argparse
import json
from pathlib import Path
import sys

from pdf_to_ofx.corpus.batch import analyze_corpus
from pdf_to_ofx.corpus.report import aggregate_report, render_report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Corpus Lab V1: local structural coverage measurements.")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyze a local JSONL manifest without exporting OFX")
    analyze.add_argument("manifest", type=Path)
    analyze.add_argument("-o", "--output", type=Path, default=Path(".corpus/reports/latest.jsonl"))
    report = commands.add_parser("report", help="Aggregate unique document outcomes and structural failures")
    report.add_argument("input", type=Path)
    report.add_argument("--json", action="store_true", help="Print the complete aggregate and capability matrix")
    report.add_argument("--top", type=int, default=10, help="Maximum rows in each human-readable cluster/matrix section")
    args = parser.parse_args(argv)
    if args.command == "report" and args.top < 1:
        parser.error("--top must be positive")
    try:
        if args.command == "analyze":
            summary = analyze_corpus(args.manifest, args.output)
            print(f"Corpus batch: {summary.entries} entries; {summary.analyzed} analyzed; "
                  f"{summary.duplicates} duplicates; {summary.input_errors} input errors")
            return 0 if summary.input_errors == 0 else 1
        aggregate = aggregate_report(args.input)
        print(json.dumps(aggregate, indent=2, sort_keys=True) if args.json else render_report(aggregate, args.top))
        return 0 if not aggregate["input_errors"] else 1
    except (OSError, ValueError):
        print("Corpus command failed: check the local input and JSONL output configuration.", file=sys.stderr)
        return 1
