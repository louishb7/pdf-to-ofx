"""Fictitious regressions for complete lexical ownership before reconciliation."""

from dataclasses import replace
from decimal import Decimal

import pytest

from pdf_to_ofx.application.convert import analyze_pdf, assess_export_readiness, export_analysis, ExportStatus
from pdf_to_ofx.cli import main
from pdf_to_ofx.domain.errors import ConversionError
from pdf_to_ofx.domain.evidence import AnalysisStatus, InferenceDiagnostic
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.semantics import money_regions, parse_money
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens


PERIOD = "Período: 01/10/2026 a 31/10/2026\n"
MALFORMED = (
    "1 234,56", "12 345,67", "1,234.56", "$1,234.56", "$ 1,234.56",
    "€1.234,56", "€ 1.234,56", "1.234,56 EUR", "(1.234,56)",
    "( 1.234,56 )", "ABC1.234,56XYZ", "1 , 234,56",
    "R$ 1 234,56", "- R$ 1 234,56", "1.234,56 XYZ",
    "¥ 1.234,56", "− 1.234,56", "-1.234,56 C",
    "-1 234,56", "+1 234,56", "R$1 234,56", "( -1.234,56 )",
)


@pytest.mark.parametrize("value", MALFORMED)
def test_incomplete_expression_never_exposes_a_valid_suffix(make_document, value):
    row = reconstruct_rows(make_document(value))[0]
    assert parse_money(value) is None
    assert money_regions(row, Tolerances()) == ()


@pytest.mark.parametrize("value", MALFORMED)
@pytest.mark.parametrize("field", ("opening", "closing", "movement"))
def test_incomplete_expression_blocks_analysis_before_hypotheses(make_document, value, field):
    opening = value if field == "opening" else "0,00"
    closing = value if field == "closing" else "1.234,56"
    movement = value if field == "movement" else "+1.234,56"
    document = make_document(PERIOD + f"Saldo inicial: {opening}\nSaldo final: {closing}\n"
                             f"05/10/2026 TESTE {movement}")
    result = infer_layout(document)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.diagnostic == InferenceDiagnostic.CAPABILITY_MISSING
    assert result.blocking_capabilities == ("monetary_token_incomplete",)
    assert result.interpretation is None
    assert result.candidate_hypotheses == result.explored_hypotheses == 0
    assert result.monetary_diagnostics
    source = result.monetary_diagnostics[0].source
    assert " ".join(source_tokens(document, source)) == value
    assert value not in result.reason


@pytest.mark.parametrize("mode", ({}, {"legacy": True}, {"operators": True}))
def test_matching_truncated_balances_cannot_pass_any_inference_path(make_document, mode):
    document = make_document(PERIOD + "Saldo inicial: 1 234,56\nSaldo final: 1 244,56\n"
                             "05/10/2026 TESTE +10,00")
    result = infer_layout(document, **mode)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert "monetary_token_incomplete" in result.blocking_capabilities
    assert result.interpretation is None


@pytest.mark.parametrize("description", (
    "DOCUMENTO 123", "REFERÊNCIA 123456789", "LOTE 12 ITEM 345",
    "DOC123456", "NF 2026/123", "PIX", "DOCUMENTO 1.234",
))
def test_description_numbers_remain_description_with_explicit_amount_boundary(make_document, description):
    document = make_document(PERIOD + "Saldo inicial: 0,00\nSaldo final: 10,00\n"
                             f"05/10/2026 {description} +R$ 10,00")
    result = infer_layout(document)
    assert result.status == AnalysisStatus.SUCCESS
    statement = result.interpretation.statement
    assert len(statement.transactions) == 1
    assert statement.transactions[0].description == description
    assert statement.transactions[0].amount == Decimal("10.00")
    source = result.interpretation.provenance.transactions[0]
    assert " ".join(t for s in source.description_sources for t in source_tokens(document, s)) == description
    assert source_tokens(document, source.amount_source) == ("+R$", "10,00")


def test_reference_without_money_is_not_inventoried(make_document):
    row = reconstruct_rows(make_document("DOCUMENTO 123456789 LOTE 12 ITEM 345"))[0]
    assert money_regions(row, Tolerances()) == ()


@pytest.mark.parametrize("value,expected", (
    ("R$ 1.234,56", "1234.56"), ("- R$ 1.234,56", "-1234.56"),
    ("+R$ 1.234,56", "1234.56"), ("1.234,56 C", "1234.56"),
    ("1.234,56 D", "-1234.56"), ("0,00", "0.00"),
))
def test_supported_tokens_keep_full_original_spans(make_document, value, expected):
    row = reconstruct_rows(make_document(value))[0]
    regions = money_regions(row, Tolerances())
    assert len(regions) == 1
    region = regions[0]
    assert (region.start, region.end) == (0, len(row.words))
    assert region.money.amount == Decimal(expected)


@pytest.mark.parametrize("value", ("1\u00a0234,56", "1'234,56", "1_234,56"))
def test_other_separators_remain_unsupported(make_document, value):
    assert parse_money(value) is None
    assert money_regions(reconstruct_rows(make_document(value))[0], Tolerances()) == ()


def test_separate_reference_column_is_not_a_numeric_prefix(make_document):
    document = make_document(PERIOD + "Saldo inicial: 0,00\nSaldo final: 10,00\n"
                             "05/10/2026 DOCUMENTO 123 10,00")
    page = document.pages[0]
    document = replace(document, pages=(replace(page, words=tuple(
        replace(w, x0=w.x0 + 30, x1=w.x1 + 30) if w.text == "10,00" and w.top == 74 else w
        for w in page.words)),))
    result = infer_layout(document)
    assert result.status == AnalysisStatus.SUCCESS
    assert result.interpretation.statement.transactions[0].description == "DOCUMENTO 123"


@pytest.mark.parametrize("affix", ("-", "+", "R$", "€"))
def test_unbound_monetary_prefix_never_reaches_a_transaction(make_document, affix):
    document = make_document(PERIOD + "Saldo inicial: 0,00\nSaldo final: 10,00\n"
                             f"05/10/2026 TESTE {affix} 10,00")
    page = document.pages[0]
    document = replace(document, pages=(replace(page, words=tuple(
        replace(w, x0=w.x0 + 30, x1=w.x1 + 30) if w.text == "10,00" and w.top == 74 else w
        for w in page.words)),))
    result = infer_layout(document)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ("monetary_token_incomplete",)
    assert result.interpretation is None


def test_integrity_failure_is_stable_under_translation_and_candidate_inversion(make_document):
    document = make_document(PERIOD + "Saldo inicial: 1 234,56\nSaldo final: 1 244,56\n"
                             "05/10/2026 TESTE +10,00")
    translated = replace(document, pages=tuple(replace(page, words=tuple(
        replace(w, x0=w.x0 + 25, x1=w.x1 + 25, top=w.top + 13, bottom=w.bottom + 13)
        for w in page.words)) for page in document.pages))
    before = infer_layout(document)
    after = infer_layout(translated, reverse_candidates=True)
    assert before.status == after.status == AnalysisStatus.UNSUPPORTED
    assert before.blocking_capabilities == after.blocking_capabilities == ("monetary_token_incomplete",)
    assert before.monetary_diagnostics == after.monetary_diagnostics


def test_actual_pdf_reconciled_truncation_cannot_export(write_pdf, tmp_path, capsys):
    path = tmp_path / "partial-balances.pdf"
    write_pdf(PERIOD + "Saldo inicial: 1 234,56\nSaldo final: 1 244,56\n"
              "05/10/2026 TESTE +10,00", path)
    document = extract_pdf(path)
    result = infer_layout(document)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert source_tokens(document, result.monetary_diagnostics[0].source) == ("1", "234,56")
    analysis = analyze_pdf(path)
    assert analysis.status == AnalysisStatus.UNSUPPORTED
    assert analysis.statement is None
    assert analysis.blocking_capabilities == ("monetary_token_incomplete",)
    assert assess_export_readiness(analysis).status == ExportStatus.EXPORT_BLOCKED
    with pytest.raises(ConversionError):
        export_analysis(analysis)
    output = tmp_path / "must-not-exist.ofx"
    assert main([str(path), "-o", str(output)]) == 1
    assert not output.exists()
    assert "234,56" not in capsys.readouterr().err
