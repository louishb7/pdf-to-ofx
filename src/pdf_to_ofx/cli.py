"""Developer CLI; displays summaries without transaction descriptions."""

import argparse
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from pdf_to_ofx.application.convert import convert_pdf
from pdf_to_ofx.domain.errors import ConversionError


def _write_output(output: Path, contents: str) -> None:
    # Publish a complete file atomically, without overwriting an existing path.
    # The temporary file also defaults to owner-only permissions on POSIX.
    with NamedTemporaryFile(dir=output.parent, prefix=".pdf-to-ofx-", delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_text(contents, encoding="ascii", newline="\n")
        output.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert the synthetic digital PDF to provisional OFX.")
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
    opening = format(statement.opening_balance, ".2f") if statement.opening_balance is not None else "unavailable"
    closing = format(statement.closing_balance, ".2f") if statement.closing_balance is not None else "unavailable"
    print(f"Opening balance: {opening}; closing balance: {closing}")
    print("Validation: passed")
    print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
