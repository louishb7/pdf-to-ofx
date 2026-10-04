"""Fictitious structural recipes keep exact finances under harmless changes."""

from dataclasses import replace
from pathlib import Path

import pytest

from pdf_to_ofx.domain.evidence import AnalysisStatus
from pdf_to_ofx.generic.composition import interpret_composed
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.pages import continue_pages
from pdf_to_ofx.generic.structure import reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage
from pdf_to_ofx.pdf.extractor import extract_pdf


LAYOUTS = Path(__file__).parents[1] / "fixtures/layouts"


def transform(document, operation):
    pages = []
    for page in document.pages:
        offsets = {}
        for row in reconstruct_rows(ExtractedDocument((page,))):
            offsets.update({id(word): index * 0.2 for index, word in enumerate(row.words)})
        words = []
        for word in page.words:
            scale = 1.01 if operation == "scale" else 1
            dx = 13 if operation == "horizontal" else offsets[id(word)] if operation == "spacing" else 0
            dy = 7 if operation == "vertical" else 0
            words.append(replace(word, x0=word.x0 * scale + dx, x1=word.x1 * scale + dx,
                                 top=word.top * scale + dy, bottom=word.bottom * scale + dy))
        pages.append(replace(page, words=tuple(words), width=page.width * scale, height=page.height * scale))
    return replace(document, pages=tuple(pages))


@pytest.mark.parametrize("recipe", ["grouped_signed_balance", "per_row_signed_balance", "grouped_subtotals_wrapped"])
@pytest.mark.parametrize("operation", ["horizontal", "vertical", "spacing", "scale"])
def test_financial_interpretation_and_evidence_survive_small_geometric_changes(recipe, operation, write_layout_pdf, tmp_path):
    path = tmp_path / "synthetic.pdf"
    write_layout_pdf(LAYOUTS / recipe / "pages.json", path)
    document = extract_pdf(path)
    before, after = infer_layout(document), infer_layout(transform(document, operation))
    assert before.status == after.status == AnalysisStatus.SUCCESS
    assert before.interpretation.statement == after.interpretation.statement
    assert before.interpretation.evidence == after.interpretation.evidence
    # Provenance is structurally stable for geometric transforms.
    assert before.interpretation.provenance == after.interpretation.provenance


def repaginate(document, profile, boundary):
    rows = continue_pages(reconstruct_rows(document, profile.tolerances), profile)
    pages = []
    for number, group in enumerate((rows[:boundary], rows[boundary:]), 1):
        words = []
        for index, row in enumerate(group):
            offset = 20 + index * 18 - row.words[0].top
            words.extend(replace(w, page=number, top=w.top + offset, bottom=w.bottom + offset) for w in row.words)
        pages.append(ExtractedPage(number, "", tuple(words), 595, 842))
    return ExtractedDocument(tuple(pages))


@pytest.mark.parametrize("recipe", ["grouped_signed_balance", "grouped_subtotals_wrapped"])
@pytest.mark.parametrize("split", ["before_movement", "after_movement"])
def test_different_page_break_preserves_transactions_and_all_financial_controls(recipe, split, write_layout_pdf, tmp_path):
    path = tmp_path / "synthetic.pdf"
    write_layout_pdf(LAYOUTS / recipe / "pages.json", path)
    document = extract_pdf(path)
    result = infer_layout(document)
    profile = result.profile
    # Put a page boundary on either side of the first movement. In the wrapped
    # layout this also moves the description continuation to the second page.
    rows = continue_pages(reconstruct_rows(document), profile)
    from pdf_to_ofx.generic.operators.transactions import semantic_candidates
    candidates = semantic_candidates(rows, profile)
    start = next(c.position for c in candidates if c.dates and c.kind in {"date_heading", "flow"})
    first_movement = next(c.position for c in candidates[start:] if c.kind == "content" and c.amounts)
    boundary = first_movement + (1 if split == "after_movement" else 0)
    altered = repaginate(document, profile, boundary)
    portable = replace(profile, footer_rows=0, repeated_header_rows=0, trailing_note_rows=0)
    composed = interpret_composed(altered, portable)
    reinferred = infer_layout(altered)
    assert reinferred.status == AnalysisStatus.SUCCESS
    assert composed.statement == result.interpretation.statement == reinferred.interpretation.statement
    assert composed.evidence == result.interpretation.evidence == reinferred.interpretation.evidence
    if split == "after_movement" and recipe == "grouped_subtotals_wrapped":
        source = composed.provenance.transactions[0]
        assert [s.page for s in source.description_sources] == [1, 2]
        assert source.date_source.page == 1


def test_exceeding_continuation_alignment_limit_abstains_without_changed_finances(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    result = infer_layout(document)
    profile = result.profile
    pages = tuple(replace(page, words=tuple(
        replace(word, x0=word.x0 + 30, x1=word.x1 + 30)
        if page.number == 2 and word.top < 95 and word.top > 70 else word
        for word in page.words)) for page in document.pages)
    altered = replace(document, pages=pages)
    with pytest.raises(OperatorFailure, match="continuation"):
        interpret_composed(altered, profile)
    assert infer_layout(altered).status != AnalysisStatus.SUCCESS


def test_exceeding_row_tolerance_never_silently_changes_financial_interpretation(layout_pdf):
    document = extract_pdf(layout_pdf)
    before = infer_layout(document)
    # Move only the value columns far from their description row.
    pages = tuple(replace(page, words=tuple(
        replace(word, top=word.top + 6, bottom=word.bottom + 6)
        if word.x0 >= 330 else word for word in page.words)) for page in document.pages)
    after = infer_layout(replace(document, pages=pages))
    assert after.status != AnalysisStatus.SUCCESS or after.interpretation.statement == before.interpretation.statement
