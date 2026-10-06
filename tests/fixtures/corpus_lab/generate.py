"""Generate a reproducible, entirely fictitious development corpus."""

import argparse
import json
from pathlib import Path
import shutil

from reportlab.pdfgen.canvas import Canvas


def generate_corpus(output: Path) -> Path:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Fictitious corpus generation requires a new or empty directory.")
    fixtures = Path(__file__).parent
    raw = output / "raw"
    manifests = output / "manifests"
    raw.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    (output / "reports").mkdir(parents=True, exist_ok=True)
    for name, text in json.loads((fixtures / "documents.json").read_text()).items():
        canvas = Canvas(str(raw / f"{name}.pdf"), pagesize=(595, 842), invariant=1, pageCompression=0)
        canvas.setTitle("Fictitious Corpus Lab fixture")
        canvas.setAuthor("Synthetic data only")
        canvas.setFont("Helvetica", 9)
        for index, line in enumerate(text.splitlines()):
            canvas.drawString(32, 810 - index * 18, line)
        canvas.save()
    shutil.copyfile(raw / "success.pdf", raw / "duplicate.pdf")
    manifest = manifests / "corpus.jsonl"
    shutil.copyfile(fixtures / "corpus.jsonl", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Directory for the fictitious raw/manifests/reports tree")
    generate_corpus(parser.parse_args().output)
