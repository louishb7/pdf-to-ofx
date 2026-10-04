"""Fictitious positioned documents and real extraction of structural recipes."""

import importlib.util
from pathlib import Path

import pytest

from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage, Word

LAYOUTS = Path(__file__).parents[1] / "fixtures" / "layouts"


@pytest.fixture
def make_document():
    def build(*pages: str) -> ExtractedDocument:
        extracted = []
        for number, text in enumerate(pages, 1):
            words = []
            for index, line in enumerate(text.splitlines()):
                x = 20
                for token in line.split():
                    width = len(token) * 6
                    words.append(Word(token, x, x + width, 20 + index * 18, 30 + index * 18, number))
                    x += width + 4
            extracted.append(ExtractedPage(number, text, tuple(words)))
        return ExtractedDocument(tuple(extracted))
    return build


@pytest.fixture
def write_layout_pdf():
    spec = importlib.util.spec_from_file_location("layout_fixture_writer", LAYOUTS / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_layout_pdf


@pytest.fixture(params=["grouped_signed_balance", "per_row_signed_balance"])
def layout_pdf(request, write_layout_pdf, tmp_path):
    output = tmp_path / "statement.pdf"
    write_layout_pdf(LAYOUTS / request.param / "pages.json", output)
    return output
