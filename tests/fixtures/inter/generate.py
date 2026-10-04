"""Write the public, wholly fictitious Inter layout fixture reproducibly."""

import argparse
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas


def write_pdf(pages: tuple[str, ...], output: Path) -> None:
    canvas = Canvas(str(output), pagesize=(595, 842), invariant=1, pageCompression=0)
    canvas.setTitle("Fictitious Banco Inter layout M1 fixture")
    canvas.setAuthor("Synthetic test data only")
    for page in pages:
        cursor = canvas.beginText(35, 800)
        cursor.setFont("Helvetica", 9)
        cursor.setLeading(18)
        for line in page.splitlines():
            cursor.textLine(line)
        canvas.drawText(cursor)
        canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("statement.pdf"))
    args = parser.parse_args()
    directory = Path(__file__).parent
    write_pdf(tuple((directory / f"page{number}.txt").read_text(encoding="utf-8") for number in (1, 2)), args.output)
