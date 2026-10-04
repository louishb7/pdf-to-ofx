"""Available financial proof, chronological declarations and source ownership."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf
from pdf_to_ofx.domain.errors import FinancialCoverageError, StatementValidationError
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus, FinancialRole, SourceSpan
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.parser import GenericStatementParser, strip_footers
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.validation.statement import validate_domain, validate_statement


def test_domain_validity_without_balances_is_not_reconciliation(statement):
    plain = replace(statement, opening_balance=None, closing_balance=None)
    validate_domain(plain)
    evidence = validate_statement(plain)
    assert evidence.domain_valid
    assert evidence.opening_closing_reconciled == EvidenceStatus.NOT_AVAILABLE
    assert evidence.running_balance_verified == EvidenceStatus.NOT_AVAILABLE
    assert evidence.reconciliation_level == "none"
    assert not evidence.has_financial_support
    assert not replace(evidence, running_balance_links=1).has_financial_support


@pytest.mark.parametrize("identity", ["", "inter", "unknown", "synthetic", "other"])
def test_financial_acceptance_and_rejection_are_identity_independent(inter_statement, identity):
    changed = replace(inter_statement, bank_id=identity)
    assert validate_statement(changed) == validate_statement(inter_statement)
    missing = replace(changed.transactions[0], balance_after=None)
    with pytest.raises(StatementValidationError, match="profile requires"):
        validate_statement(replace(changed, transactions=(missing, *changed.transactions[1:])))


def test_synthetic_evidence_explicitly_distinguishes_absence_and_not_applicable(synthetic_pdf):
    evidence = analyze_pdf(synthetic_pdf).evidence
    assert evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    assert evidence.running_balance_verified == EvidenceStatus.NOT_APPLICABLE
    assert evidence.group_subtotals_verified == EvidenceStatus.NOT_APPLICABLE
    assert evidence.credit_total_verified == EvidenceStatus.NOT_AVAILABLE
    assert evidence.financial_coverage_verified == EvidenceStatus.VERIFIED
    with pytest.raises(FrozenInstanceError):
        evidence.domain_valid = False


def test_inter_running_evidence_does_not_claim_missing_opening_balance(inter_pdf):
    evidence = analyze_pdf(inter_pdf).evidence
    assert evidence.running_balance_verified == EvidenceStatus.VERIFIED
    assert evidence.running_balance_links == 4
    assert evidence.first_movement_verified == EvidenceStatus.NOT_AVAILABLE
    assert evidence.opening_closing_reconciled == EvidenceStatus.NOT_AVAILABLE
    assert evidence.daily_balances_verified == EvidenceStatus.VERIFIED
    assert evidence.financial_coverage_verified == EvidenceStatus.VERIFIED


def test_group_subtotals_and_totals_are_independent_evidence(grouped_pdf):
    analysis = analyze_pdf(grouped_pdf)
    evidence = analysis.evidence
    assert evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    assert evidence.group_subtotals_verified == EvidenceStatus.VERIFIED
    assert evidence.credit_total_verified == EvidenceStatus.VERIFIED
    assert evidence.debit_total_verified == EvidenceStatus.VERIFIED
    assert evidence.running_balance_verified == EvidenceStatus.NOT_APPLICABLE
    assert evidence.daily_balances_verified == EvidenceStatus.NOT_AVAILABLE
    assert evidence.financial_coverage_verified == EvidenceStatus.VERIFIED


def test_domain_does_not_assume_document_order_is_economic_order(statement):
    reversed_document = replace(statement, transactions=statement.transactions[::-1], chronology=Chronology.UNDECLARED)
    evidence = validate_statement(reversed_document)
    assert evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    assert evidence.economic_order_verified == EvidenceStatus.NOT_AVAILABLE
    assert reversed_document.transactions == statement.transactions[::-1]
    with pytest.raises(StatementValidationError, match="order"):
        validate_statement(replace(reversed_document, chronology=Chronology.ASCENDING))


def test_sparse_running_balances_are_checked_only_at_source_checkpoints(inter_statement):
    transactions = tuple(replace(t, balance_after=None) if i == 2 else t
                         for i, t in enumerate(inter_statement.transactions))
    sparse = replace(inter_statement, transactions=transactions, running_balances_required=False)
    evidence = validate_statement(sparse)
    assert evidence.running_balance_links == 3
    assert sparse.transactions[2].balance_after is None
    wrong = replace(transactions[2], amount=transactions[2].amount + Decimal("0.01"))
    with pytest.raises(StatementValidationError, match="Running balance"):
        validate_statement(replace(sparse, transactions=(*transactions[:2], wrong, *transactions[3:])))


def test_unknown_economic_order_cannot_claim_running_reconciliation(inter_statement):
    evidence = validate_statement(replace(inter_statement, chronology=Chronology.UNDECLARED))
    assert evidence.domain_valid
    assert evidence.running_balance_links == 0
    assert evidence.running_balance_verified == EvidenceStatus.NOT_AVAILABLE


@pytest.mark.parametrize("fixture", ["synthetic_pdf", "inter_pdf", "coverage_pdf", "grouped_pdf"])
def test_every_transaction_occurrence_and_money_region_has_traceable_origin(request, fixture):
    source = request.getfixturevalue(fixture)
    analysis = analyze_pdf(source)
    assert analysis.status == AnalysisStatus.SUCCESS
    provenance = analysis.provenance
    assert len(provenance.transactions) == len(analysis.statement.transactions)
    movements = [a for a in provenance.monetary_regions if a.role == FinancialRole.MOVEMENT]
    assert [a.transaction_index for a in movements] == list(range(len(analysis.statement.transactions)))
    assert len({a.source for a in provenance.monetary_regions}) == len(provenance.monetary_regions)
    assert all(t.spans for t in provenance.transactions)
    if analysis.layout_profile is not None:
        rows = reconstruct_rows(extract_pdf(source), analysis.layout_profile.tolerances)
        expected = VisualCoverage(rows, analysis.layout_profile.tolerances).expected
        assert expected == {a.source for a in provenance.monetary_regions}
    # References contain indexes only, never copies of descriptions or amounts.
    assert all(isinstance(a.source, SourceSpan) for a in provenance.monetary_regions)
    assert all(not hasattr(a, "text") and not hasattr(a, "amount") for a in provenance.monetary_regions)


def test_multiline_and_cross_page_description_source_is_preserved(grouped_pdf):
    analysis = analyze_pdf(grouped_pdf)
    source = analysis.provenance.transactions[1]
    assert {s.page for s in source.spans} == {1, 2}
    assert len(source.spans) == 4  # date, flow, movement, next-page continuation


def test_cancelling_movements_cannot_disappear_behind_global_reconciliation(coverage_pdf):
    document = extract_pdf(coverage_pdf)
    inference = infer_layout(document)
    assert inference.status == AnalysisStatus.SUCCESS
    interpretation = inference.interpretation
    assert [t.amount for t in interpretation.statement.transactions] == [Decimal("20.00"), Decimal("-20.00"), Decimal("5.00")]
    omitted = replace(interpretation.statement, transactions=interpretation.statement.transactions[-1:])
    # Global arithmetic alone still approves 100 + 5 = 105.
    assert validate_statement(omitted).opening_closing_reconciled == EvidenceStatus.VERIFIED

    def weak_selection(rows, profile):
        selected = strip_footers(rows, profile)
        return tuple(row for row in selected if "CRÉDITO FICTÍCIO" not in row.text and "DÉBITO FICTÍCIO" not in row.text)

    # Original extraction still contains +20/-20. Remove only the candidate
    # interpretation's rows AFTER the coverage inventory has been constructed.
    with patch("pdf_to_ofx.generic.parser.strip_footers", side_effect=weak_selection), \
         patch("pdf_to_ofx.generic.composition.continue_pages", side_effect=weak_selection):
        with pytest.raises(FinancialCoverageError, match="Unclassified monetary"):
            GenericStatementParser().interpret(document, inference.profile)
        assert infer_layout(document, legacy=True).status == AnalysisStatus.INVALID
        assert analyze_pdf(coverage_pdf).status == AnalysisStatus.INVALID
    assert len(document.pages[0].words) == len(extract_pdf(coverage_pdf).pages[0].words)


def test_grouped_candidate_cannot_hide_monetary_header(grouped_pdf):
    import pdf_to_ofx.generic.grouped as grouped
    document = extract_pdf(grouped_pdf)
    profile = infer_layout(document).profile
    frame = grouped._frame

    def suppress_control(rows, selected_profile):
        framed = frame(rows, selected_profile)
        return tuple(row for row in framed if "Total de créditos" not in row.text or "01 ABR" in row.text)

    with patch.object(grouped, "_frame", side_effect=suppress_control):
        with pytest.raises(FinancialCoverageError, match="Unclassified monetary"):
            GenericStatementParser().interpret(document, profile)


def test_regions_cannot_be_claimed_twice(coverage_pdf):
    rows = reconstruct_rows(extract_pdf(coverage_pdf))
    coverage = VisualCoverage(rows, Tolerances())
    source = next(iter(coverage.expected))
    coverage.claim(source, FinancialRole.OPENING_BALANCE)
    with pytest.raises(FinancialCoverageError):
        coverage.claim(source, FinancialRole.CLOSING_BALANCE)


@pytest.mark.parametrize("args", [(0, 1, 0, 1), (1, 0, 0, 1), (1, 1, -1, 1), (1, 1, 2, 2)])
def test_source_spans_reject_invalid_indexes(args):
    with pytest.raises(ValueError):
        SourceSpan(*args)
