"""Interpretation states and identity-free application behavior."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from pdf_to_ofx.application.convert import (
    ExportStatus, StatementAnalysis, analyze_pdf, assess_export_readiness, convert_pdf, export_analysis,
)
from pdf_to_ofx.domain.errors import ConversionError, MissingOFXMetadataError, MissingOFXRequirementsError
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus
from pdf_to_ofx.domain.models import BankAccount
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.parser import StatementContext

ACCOUNT = BankAccount("Instituição Fictícia", "999", "999", "0001", "DEMO-0001")
HEADER = "Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\nSaldo final: R$ 100,01\n"
BODY = "01/03/2027 ALFA +R$ 0,02 R$ 100,02\n02/03/2027 BETA -R$ 0,01 R$ 100,01"


@pytest.mark.parametrize("text,status", [
    (HEADER + BODY, AnalysisStatus.SUCCESS),
    ("Período: 01/03/2027 a 03/03/2027\n01/03/2027 ALFA R$ 0,01 -R$ 0,01\n02/03/2027 BETA R$ 0,02 R$ 0,01", AnalysisStatus.AMBIGUOUS),
    ("DOCUMENTO DEMONSTRATIVO SEM ESTRUTURA FINANCEIRA", AnalysisStatus.UNSUPPORTED),
    (HEADER + BODY.replace("-R$ 0,01", "-R$ 0,02"), AnalysisStatus.INVALID),
])
def test_analysis_states_without_any_ofx_generation(make_document, text, status):
    with patch("pdf_to_ofx.application.convert.extract_pdf", return_value=make_document(text)), \
         patch("pdf_to_ofx.application.convert.generate_ofx", side_effect=AssertionError("No OFX during analysis")):
        analysis = analyze_pdf(None)
    assert analysis.status == status
    assert (analysis.statement is not None) == (status == AnalysisStatus.SUCCESS)
    if status == AnalysisStatus.AMBIGUOUS:
        assert analysis.ambiguities
    if status != AnalysisStatus.SUCCESS:
        assert assess_export_readiness(analysis, ACCOUNT).status == ExportStatus.EXPORT_BLOCKED
        with pytest.raises(ConversionError):
            export_analysis(analysis, ACCOUNT)


@pytest.mark.parametrize("fixture", ["synthetic_pdf", "inter_pdf", "coverage_pdf", "grouped_pdf"])
def test_metadata_and_bank_identity_do_not_change_analysis(request, fixture):
    source = request.getfixturevalue(fixture)
    plain = analyze_pdf(source)
    assert plain.status == AnalysisStatus.SUCCESS
    for bank in ("", "unknown", "inter", "synthetic", "any-identity"):
        enriched = analyze_pdf(source, context=StatementContext(bank_id=bank, account=ACCOUNT))
        assert enriched == plain  # Includes transactions, dates, evidence and provenance.


def test_readiness_is_separate_and_uses_actual_export_requirements(grouped_pdf):
    analysis = analyze_pdf(grouped_pdf)
    original = analysis
    with patch("pdf_to_ofx.application.convert.generate_ofx", side_effect=AssertionError("No payload in readiness")):
        assert assess_export_readiness(analysis).status == ExportStatus.EXPORT_METADATA_REQUIRED
        assert assess_export_readiness(analysis, ACCOUNT).status == ExportStatus.READY_TO_EXPORT
    assert analysis == original
    with pytest.raises(MissingOFXMetadataError):
        export_analysis(analysis)
    exported = export_analysis(analysis, ACCOUNT)
    assert exported == export_analysis(analysis, ACCOUNT)
    root = ET.fromstring(exported.split("\n\n", 1)[1])
    assert len(root.findall(".//STMTTRN")) == 4
    assert root.findtext(".//ACCTID") == ACCOUNT.account_id
    assert analysis.statement.account is None
    with pytest.raises(FrozenInstanceError):
        analysis.status = AnalysisStatus.INVALID


@pytest.mark.parametrize("field", ["bank_id", "account_id", "branch_id", "organization", "institution_id"])
def test_incomplete_metadata_only_blocks_export(grouped_pdf, field):
    analysis = analyze_pdf(grouped_pdf)
    account = replace(ACCOUNT, **{field: ""})
    assert analysis.status == AnalysisStatus.SUCCESS
    assert assess_export_readiness(analysis, account).status == ExportStatus.EXPORT_METADATA_REQUIRED


def test_complete_identity_does_not_fill_missing_closing_balance(make_document):
    document = make_document((HEADER + BODY).replace("Saldo final: R$ 100,01\n", ""))
    with patch("pdf_to_ofx.application.convert.extract_pdf", return_value=document):
        analysis = analyze_pdf(None)
    assert analysis.status == AnalysisStatus.SUCCESS
    assert analysis.evidence.opening_closing_reconciled == EvidenceStatus.NOT_AVAILABLE
    assert assess_export_readiness(analysis, ACCOUNT).status == ExportStatus.EXPORT_REQUIREMENTS_MISSING
    with pytest.raises(MissingOFXRequirementsError):
        export_analysis(analysis, ACCOUNT)


def test_explicit_profile_does_not_bypass_minimum_evidence(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\nSaldo final: R$ 100,01\n01/03/2027 ALFA R$ 0,01 R$ 100,01")
    from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    with patch("pdf_to_ofx.application.convert.extract_pdf", return_value=document):
        assert analyze_pdf(None, layout_profile=profile).status == AnalysisStatus.AMBIGUOUS


@pytest.mark.parametrize("fixture", ["synthetic_pdf", "inter_pdf"])
def test_traditional_conversion_and_analysis_export_have_identical_bytes(request, fixture):
    source = request.getfixturevalue(fixture)
    analysis = analyze_pdf(source)
    assert assess_export_readiness(analysis).status == ExportStatus.READY_TO_EXPORT
    assert export_analysis(analysis).encode("ascii") == convert_pdf(source).ofx.encode("ascii")


def test_conflicting_identity_never_changes_or_discards_interpretation(inter_pdf):
    analysis = analyze_pdf(inter_pdf, context=StatementContext(account=ACCOUNT))
    assert analysis.status == AnalysisStatus.SUCCESS
    assert len(analysis.statement.transactions) == 5
    assert assess_export_readiness(analysis, ACCOUNT).status == ExportStatus.EXPORT_REQUIREMENTS_MISSING
    with pytest.raises(ConversionError, match="contradicts"):
        export_analysis(analysis, ACCOUNT)


def test_unverified_domain_model_cannot_masquerade_as_approved(statement):
    fake = StatementAnalysis(AnalysisStatus.SUCCESS, statement)
    assert assess_export_readiness(fake).status == ExportStatus.EXPORT_BLOCKED
    with pytest.raises(ConversionError, match="evidence"):
        export_analysis(fake)


def test_generic_inference_identity_never_selects_another_hypothesis(make_document):
    document = make_document(HEADER + BODY)
    plain = infer_layout(document)
    enriched = infer_layout(document, context=StatementContext(bank_id="inter", account=ACCOUNT))
    assert plain == enriched


def test_running_layout_identity_independence(layout_pdf):
    plain = analyze_pdf(layout_pdf)
    assert analyze_pdf(layout_pdf, context=StatementContext(bank_id="inter", account=ACCOUNT)) == plain


def test_export_rejects_statement_replaced_with_fewer_transactions_despite_valid_global_sum(coverage_pdf):
    analysis = analyze_pdf(coverage_pdf)
    altered = replace(analysis, statement=replace(analysis.statement, transactions=analysis.statement.transactions[-1:]))
    assert assess_export_readiness(altered, ACCOUNT).status == ExportStatus.EXPORT_BLOCKED
    with pytest.raises(ConversionError, match="coverage"):
        export_analysis(altered, ACCOUNT)


def test_readiness_rechecks_financial_values_in_an_altered_analysis(synthetic_pdf):
    analysis = analyze_pdf(synthetic_pdf)
    altered = replace(analysis, statement=replace(analysis.statement, closing_balance=Decimal("0.00")))
    assert assess_export_readiness(altered).status == ExportStatus.EXPORT_BLOCKED
