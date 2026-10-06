"""Developer CLI; displays summaries without transaction descriptions."""

import argparse
import json
import sys
from pathlib import Path

from pdf_to_ofx.application.convert import convert_pdf
from pdf_to_ofx.application.export import write_ofx as _write_output
from pdf_to_ofx.domain.errors import ConversionError


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "diagnose":
        from pdf_to_ofx.application.diagnose import diagnose_pdf

        parser = argparse.ArgumentParser(description="Diagnose a local PDF without exposing financial contents or writing OFX.")
        parser.add_argument("input", type=Path, help="Local statement PDF")
        args = parser.parse_args(argv[1:])
        report = diagnose_pdf(args.input)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["approved"] else 1
    parser = argparse.ArgumentParser(description="Convert a supported digital statement PDF to provisional OFX.")
    parser.add_argument("input", type=Path, help="Local text-based statement PDF")
    parser.add_argument("-o", "--output", type=Path, help="Output file (default: input with .ofx suffix)")
    args = parser.parse_args(argv)
    try:
        if not args.input.name:
            raise ConversionError("Input must name a local PDF file.")
        output = args.output if args.output is not None else args.input.with_suffix(".ofx")
        result = convert_pdf(args.input)
        _write_output(output, result.ofx)
    except ConversionError as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("Output already exists; choose a new output path.", file=sys.stderr)
        return 1
    except OSError:
        print("Unable to write the output file; check its directory and permissions.", file=sys.stderr)
        return 1
    statement = result.statement
    credits = sum(transaction.amount > 0 for transaction in statement.transactions)
    debits = sum(transaction.amount < 0 for transaction in statement.transactions)
    print(f"Bank: {result.bank_name} / {statement.layout_id}")
    print(f"Transactions: {len(statement.transactions)} (credits: {credits}, debits: {debits})")
    if statement.bank_id == "synthetic":
        opening = format(statement.opening_balance, ".2f") if statement.opening_balance is not None else "unavailable"
        closing = format(statement.closing_balance, ".2f") if statement.closing_balance is not None else "unavailable"
        print(f"Opening balance: {opening}; closing balance: {closing}")
    if statement.bank_id == "inter" and statement.opening_balance is None:
        print("Balance validation: running balances and closing balance checked; opening balance unavailable")
    print("Validation: passed")
    print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
