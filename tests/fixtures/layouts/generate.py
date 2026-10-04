"""Write positioned, fictitious structural cells with deterministic PDF bytes."""

import argparse
import json
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas


def write_layout_pdf(recipe: Path, output: Path) -> None:
    pages = json.loads(recipe.read_text(encoding="utf-8"))["pages"]
    canvas = Canvas(str(output), pagesize=(595, 842), invariant=1, pageCompression=0)
    canvas.setTitle("Fictitious structural statement fixture")
    canvas.setAuthor("Synthetic data only")
    for page in pages:
        canvas.setFont("Helvetica", 9)
        for row in page:
            for x, text in row["cells"]:
                canvas.drawString(x, 842 - row["y"], text)
        canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_layout_pdf(args.recipe, args.output)
