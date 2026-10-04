"""Reproduce the fictitious committed PDF; ReportLab is a test-only dependency."""

import argparse
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas


def write_pdf(text: str, output: Path) -> None:
    # Fixed metadata/timestamps and uncompressed streams make regeneration stable.
    canvas = Canvas(str(output), pagesize=(595, 842), invariant=1, pageCompression=0)
    canvas.setTitle("Synthetic Bank M0 fixture")
    canvas.setAuthor("Fictitious test data")
    cursor = canvas.beginText(40, 800)
    cursor.setFont("Courier", 10)
    cursor.setLeading(18)
    for line in text.splitlines():
        cursor.textLine(line)
    canvas.drawText(cursor)
    canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("statement.pdf"))
    args = parser.parse_args()
    write_pdf(Path(__file__).with_name("statement.txt").read_text(encoding="utf-8"), args.output)
