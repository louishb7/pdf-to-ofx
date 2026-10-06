"""Column ownership of punctuation, using only fictitious PDF contents."""

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from conftest import LAYOUTS
from pdf_to_ofx.application.convert import analyze_pdf, assess_export_readiness, export_analysis, ExportStatus
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.ofx.generator import OFXProfile
from pdf_to_ofx.pdf.extractor import extract_pdf


RECIPE = LAYOUTS / "wrapped_description_separators/pages.json"


def write_variant(tmp_path, write_layout_pdf, mutate=None):
    recipe = json.loads(RECIPE.read_text())
    if mutate:
        mutate(recipe["pages"][0])
    source = tmp_path / "recipe.json"
    source.write_text(json.dumps(recipe))
    pdf = tmp_path / "fictitious.pdf"
    write_layout_pdf(source, pdf)
    return pdf


def test_wrapped_punctuation_preserves_description_and_financial_coverage(tmp_path, write_layout_pdf):
    pdf = write_variant(tmp_path, write_layout_pdf)
    analysis = analyze_pdf(pdf)
    assert analysis.status == AnalysisStatus.SUCCESS
    statement = analysis.statement
    assert [t.amount for t in statement.transactions] == [Decimal("17.25"), Decimal("-4.15")]
    assert statement.transactions[0].description == "ITEM ALFA REFERENCIA - UNIDADE - COMPLEMENTO FICTICIO"
    assert statement.transactions[1].description == "ITEM BETA DOCUMENTO - EXEMPLO - DETALHE INVENTADO"
    assert analysis.evidence.financial_coverage_verified == EvidenceStatus.VERIFIED
    assert analysis.evidence.group_subtotals_verified == EvidenceStatus.VERIFIED
    assert analysis.evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    assert all(s.direction_basis == "signed_group_subtotal" for s in analysis.provenance.transactions)
    assert assess_export_readiness(analysis).status == ExportStatus.EXPORT_METADATA_REQUIRED
    metadata = OFXProfile("999", "FICTITIOUS-ONLY", branch_id="1234",
                          organization="Fictitious Institution", institution_id="999")
    assert assess_export_readiness(analysis, metadata).status == ExportStatus.READY_TO_EXPORT
    assert "<TRNAMT>-4.15</TRNAMT>" in export_analysis(analysis, metadata)


@pytest.mark.parametrize("change", ["no_continuation", "wrong_column", "no_inner_separator", "plus", "currency", "no_flow"])
def test_a_distant_sign_is_not_silently_discarded(tmp_path, write_layout_pdf, change):
    def mutate(rows):
        if change == "no_continuation":
            del rows[8]
        elif change == "wrong_column":
            rows[8]["cells"][0][0] = 380
        elif change == "no_inner_separator":
            rows[7]["cells"][1][1] = "REFERENCIA UNIDADE -"
        elif change in {"plus", "currency"}:
            rows[7]["cells"][1][1] = "REFERENCIA - UNIDADE " + ("+" if change == "plus" else "R$")
        elif change == "no_flow":
            rows[6]["cells"] = [[35, "02 FEV 2028"]]
    analysis = analyze_pdf(write_variant(tmp_path, write_layout_pdf, mutate))
    assert analysis.status != AnalysisStatus.SUCCESS
    assert analysis.statement is None
    assert assess_export_readiness(analysis).status == ExportStatus.EXPORT_BLOCKED


def test_changed_subtotal_cannot_justify_description_punctuation(tmp_path, write_layout_pdf):
    def mutate(rows):
        rows[6]["cells"][2][1] = "+17,24"
    analysis = analyze_pdf(write_variant(tmp_path, write_layout_pdf, mutate))
    assert analysis.status != AnalysisStatus.SUCCESS
    assert analysis.statement is None


def test_column_translation_and_candidate_order_preserve_result(tmp_path, write_layout_pdf):
    document = extract_pdf(write_variant(tmp_path, write_layout_pdf))
    shifted = replace(document, pages=tuple(replace(p, words=tuple(
        replace(w, x0=w.x0 + 27, x1=w.x1 + 27, top=w.top + 11, bottom=w.bottom + 11)
        for w in p.words)) for p in document.pages))
    original = infer_layout(document)
    other = infer_layout(shifted, reverse_candidates=True)
    assert original.status == other.status == AnalysisStatus.SUCCESS
    assert original.interpretation.statement == other.interpretation.statement
    assert original.interpretation.provenance == other.interpretation.provenance
