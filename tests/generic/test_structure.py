from dataclasses import replace

import pytest

from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.generic.semantics import money_regions
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage, Word
from pdf_to_ofx.pdf.extractor import extract_pdf


def test_actual_pdf_preserves_page_and_coordinates(inter_pdf):
    document = extract_pdf(inter_pdf)
    assert len(document.pages) == 2
    for page in document.pages:
        assert page.words
        for word in page.words:
            assert word.page == page.number
            assert word.text
            assert word.x0 < word.x1 and word.top < word.bottom
    rows = reconstruct_rows(document)
    assert {row.page for row in rows} == {1, 2}
    assert rows[0].text == "Solicitado em: 04/02/2027 - 10h00"


def test_visual_rows_are_ordered_without_chaining_nearby_lines():
    words = (Word("C", 10, 15, 4, 14, 1), Word("B", 20, 25, 2, 12, 1), Word("A", 10, 15, 0, 10, 1))
    doc = ExtractedDocument((ExtractedPage(1, "", words),))
    assert [row.text for row in reconstruct_rows(doc, Tolerances(row_y=2))] == ["A B", "C"]
    assert [row.text for row in reconstruct_rows(doc, Tolerances(row_y=1))] == ["A", "B", "C"]


def test_page_boundaries_never_merge_equal_vertical_positions(make_document):
    rows = reconstruct_rows(make_document("FIRST", "SECOND"))
    assert [(row.page, row.text) for row in rows] == [(1, "FIRST"), (2, "SECOND")]


def test_currency_tokens_need_explicit_spatial_proximity():
    words = (Word("R$", 10, 20, 0, 10, 1), Word("1,00", 33, 55, 0, 10, 1))
    row = reconstruct_rows(ExtractedDocument((ExtractedPage(1, "", words),)))[0]
    assert money_regions(row, Tolerances(token_gap=13))[0].start == 0
    assert money_regions(row, Tolerances(token_gap=12))[0].start == 1


@pytest.mark.parametrize("changes", [
    {"page": 2}, {"x1": 10}, {"top": float("nan")}, {"bottom": 0}, {"text": ""},
])
def test_invalid_geometry_fails_without_exposing_text(changes):
    word = replace(Word("FICTITIOUS", 10, 20, 0, 10, 1), **changes)
    with pytest.raises(StatementParseError, match="geometry"):
        reconstruct_rows(ExtractedDocument((ExtractedPage(1, "", (word,)),)))


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_tolerances_are_rejected(value):
    with pytest.raises(ValueError):
        Tolerances(row_y=value)
