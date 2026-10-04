"""Partial monetary classification and adversarial independent controls."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf, export_analysis
from pdf_to_ofx.domain.evidence import AnalysisStatus, FinancialRole, InferenceDiagnostic, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, BankAccount
from pdf_to_ofx.generic.composition import prepare_composition
from pdf_to_ofx.generic.hypotheses import SearchBudget
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.amounts import amount_role_candidates
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.ofx.generator import account_profile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens
from pdf_to_ofx.validation.checkpoints import ConstraintViolation, check_checkpoints
from pdf_to_ofx.validation.controls import ControlInterval, validate_control_intervals

PERIOD = 'Período: 01/03/2027 a 03/03/2027\n'
MOVEMENTS = '01/03/2027 ALFA +R$ 10,00 R$ 110,00\n02/03/2027 BETA -R$ 10,00 R$ 100,00'
ACCOUNT = BankAccount('Instituição Fictícia', '999', '999', '0001', 'DEMO-0001')


def prepared_document(document):
    return prepare_composition(document, observe_geometry(document, None, Tolerances()), legacy_context=False)


def test_discovery_preserves_source_without_claiming_a_boundary_role(make_document):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' + MOVEMENTS)
    prepared = prepared_document(doc)
    assert prepared.context.opening_balance is prepared.context.closing_balance is None
    assert len(prepared.monetary_domains) == 1
    domain = prepared.monetary_domains[0]
    assert domain.roles == (FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE)
    assert domain.region.money.amount == Decimal('100')
    assert source_tokens(doc, domain.source) == ('R$', '100,00')
    assert domain.source not in {a.source for a in prepared.declarations}
    assert domain.source in VisualCoverage(prepared.original, prepared.profile.tolerances).expected


@pytest.mark.parametrize('value,expected', [('90,00', AnalysisStatus.SUCCESS), ('100,00', AnalysisStatus.AMBIGUOUS), ('95,00', AnalysisStatus.INVALID)])
def test_partial_boundary_roles_are_decided_by_existing_controls(make_document, value, expected):
    body = MOVEMENTS if value != '90,00' else MOVEMENTS.replace('-R$ 10,00 R$ 100,00', '-R$ 20,00 R$ 90,00')
    result = infer_layout(make_document(PERIOD + f'Saldo: R$ {value}\n' + body))
    assert result.status == expected
    assert not result.budget_exhausted
    if expected == AnalysisStatus.SUCCESS:
        assert result.interpretation.statement.opening_balance is None
        assert result.interpretation.statement.closing_balance == Decimal('90')
        assert result.interpretation.evidence.has_financial_support
        assert result.hypotheses[0].monetary_assignments[0].role == FinancialRole.CLOSING_BALANCE
    elif expected == AnalysisStatus.AMBIGUOUS:
        assert result.diagnostic == InferenceDiagnostic.MATERIAL_AMBIGUITY
        assert len(result.hypotheses) == 2
        assert {h.monetary_assignments[0].role for h in result.hypotheses} == {
            FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE}
    else:
        assert result.diagnostic == InferenceDiagnostic.CONSTRAINT_CONTRADICTION
        assert result.pruned_constraints and not result.hypotheses


def test_header_choice_can_be_pruned_before_a_single_transaction_assignment(make_document):
    doc = make_document(PERIOD + 'Saldo final: R$ 110,00\nSaldo: R$ 100,00\n01/03/2027 ALFA +R$ 10,00 R$ 110,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.SUCCESS
    assert dict(result.pruned_constraints)['financial_declarations'] > 0
    assert result.interpretation.statement.opening_balance == Decimal('100')
    assert result.interpretation.statement.closing_balance == Decimal('110')


@pytest.mark.parametrize('label', ['Margem desconhecida: R$ 100,00', 'Limite: R$ 0,00', 'AJUSTE R$ 10,00'])
def test_unknown_roles_are_not_invented_even_if_a_number_could_close_the_math(make_document, label):
    result = infer_layout(make_document(PERIOD + label + '\n' + MOVEMENTS))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('monetary_role_domain_empty',)
    assert result.diagnostic == InferenceDiagnostic.CAPABILITY_MISSING
    assert result.candidate_hypotheses == 0


def test_incomplete_monetary_tokens_are_not_role_ambiguity(make_document):
    result = infer_layout(make_document(PERIOD + 'AJUSTE R$ BAD\n' + MOVEMENTS))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('monetary_token_incomplete',)


def test_discovered_occurrence_cannot_receive_two_domains(make_document):
    from pdf_to_ofx.generic.operators.context import read_financial_context
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' + MOVEMENTS)

    def duplicate(*args):
        context, totals, domains = read_financial_context(*args)
        return context, totals, (*domains, *domains)

    with patch('pdf_to_ofx.generic.operators.context.read_financial_context', side_effect=duplicate):
        result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID
    assert result.blocking_capabilities == ('financial_coverage',)


def test_two_distinct_unqualified_balances_are_not_silently_merged(make_document):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\nSaldo: R$ 105,00\n' +
                        MOVEMENTS.replace('-R$ 10,00 R$ 100,00', '-R$ 5,00 R$ 105,00'))
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.SUCCESS
    roles = result.interpretation.provenance.monetary_regions
    assert len([a for a in roles if a.role == FinancialRole.OPENING_BALANCE]) == 1
    assert len([a for a in roles if a.role == FinancialRole.CLOSING_BALANCE]) == 1
    assert len(result.hypotheses[0].monetary_assignments) == 2


@pytest.mark.parametrize('budget', [SearchBudget(max_local_alternatives=1), SearchBudget(max_hypotheses=2)])
def test_boundary_domains_use_existing_budget_and_preserve_diagnostic(make_document, budget):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' + MOVEMENTS)
    a = infer_layout(doc, budget=budget)
    b = infer_layout(doc, budget=budget, reverse_candidates=True)
    assert a.status == b.status == AnalysisStatus.AMBIGUOUS
    assert a.diagnostic == b.diagnostic == InferenceDiagnostic.SEARCH_INCOMPLETE
    assert a.blocking_capabilities == b.blocking_capabilities == ('search_budget',)
    assert a.interpretation is b.interpretation is None


@pytest.mark.parametrize('value', ['90,00', '100,00', '95,00'])
def test_reversing_all_role_candidates_preserves_outcome_and_material_sources(make_document, value):
    body = MOVEMENTS if value != '90,00' else MOVEMENTS.replace('-R$ 10,00 R$ 100,00', '-R$ 20,00 R$ 90,00')
    doc = make_document(PERIOD + f'Saldo: R$ {value}\n' + body)
    a, b = infer_layout(doc), infer_layout(doc, reverse_candidates=True)
    assert a.status == b.status and a.diagnostic == b.diagnostic
    assert a.interpretation == b.interpretation
    assert a.pruned_constraints == b.pruned_constraints
    if a.interpretation:
        assert generate_ofx(a.interpretation.statement, account_profile(ACCOUNT)) == generate_ofx(b.interpretation.statement, account_profile(ACCOUNT))


def test_real_pdf_application_keeps_domain_provenance_and_deterministic_ofx(write_pdf, tmp_path):
    path = tmp_path / 'partial-balance.pdf'
    text = PERIOD + 'Saldo: R$ 90,00\n' + MOVEMENTS.replace('-R$ 10,00 R$ 100,00', '-R$ 20,00 R$ 90,00')
    write_pdf(text, path)
    analysis = analyze_pdf(path)
    assert analysis.status == AnalysisStatus.SUCCESS
    assert export_analysis(analysis, ACCOUNT) == export_analysis(analyze_pdf(path), ACCOUNT)
    closing = next(a for a in analysis.provenance.monetary_regions if a.role == FinancialRole.CLOSING_BALANCE)
    assert source_tokens(extract_pdf(path), closing.source) == ('R$', '90,00')
    assert all(s.date_source and s.description_sources and s.amount_source and s.balance_source and s.direction_source
               for s in analysis.provenance.transactions)


def test_amount_running_balance_domains_still_resolve_by_reconciliation(make_document):
    doc = make_document(PERIOD + 'Saldo inicial: R$ 100,00\nSaldo final: R$ 110,00\n'
                        '01/03/2027 ALFA R$ 110,00 +R$ 10,00')
    prepared = prepared_document(doc)
    candidates = amount_role_candidates(prepared.segments[0], prepared.profile)
    assert len(candidates) == 2
    assert candidates[0].movement == candidates[1].running_balance
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.SUCCESS
    assert result.interpretation.statement.transactions[0].amount == Decimal('10')


def test_two_transaction_amount_role_interpretations_that_close_remain_ambiguous(make_document):
    doc = make_document(PERIOD + '01/03/2027 ALFA R$ 0,01 -R$ 0,01\n02/03/2027 BETA R$ 0,02 R$ 0,01')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.MATERIAL_AMBIGUITY


def span(row, start=0, end=1, scope='main'):
    return SourceSpan(1, row, start, end, region_id=scope)


def test_repeated_controls_confirm_one_interval_without_multiplying_evidence():
    movements = (span(2), span(3))
    subtotal = ControlInterval(span(1), movements, FinancialRole.SUBTOTAL)
    duplicate = replace(subtotal, source=span(4))
    checkpoint = replace(subtotal, source=span(5), role=FinancialRole.SPARSE_CHECKPOINT)
    assert validate_control_intervals((subtotal, duplicate, checkpoint), 'main') == 1


@pytest.mark.parametrize('mutation,constraint', [
    ('source', 'control_source_reused'), ('overlapping_source', 'control_source_reused'),
    ('crossing', 'subtotal_interval_overlap'), ('nested', 'subtotal_interval_overlap'),
    ('foreign_control', 'control_scope_or_membership'), ('foreign_movement', 'control_scope_or_membership'),
    ('circular', 'circular_financial_control'), ('duplicate_movement', 'control_movement_repeated')])
def test_adversarial_subtotals_cannot_manufacture_independent_evidence(mutation, constraint):
    first = ControlInterval(span(1, 0, 2), (span(3), span(4)), FinancialRole.SUBTOTAL)
    second = replace(first, source=span(2))
    if mutation == 'source': second = first
    elif mutation == 'overlapping_source': second = replace(second, source=span(1, 1, 3))
    elif mutation == 'crossing': second = replace(second, movements=(span(4), span(5)))
    elif mutation == 'nested': second = replace(second, movements=(span(3),))
    elif mutation == 'foreign_control': second = replace(second, source=span(2, scope='other'))
    elif mutation == 'foreign_movement': second = replace(second, movements=(span(3, scope='other'),))
    elif mutation == 'circular': second = replace(second, source=span(3))
    elif mutation == 'duplicate_movement': second = replace(second, movements=(span(3), span(3)))
    with pytest.raises(ConstraintViolation, match=constraint):
        validate_control_intervals((first, second), 'main')


def test_same_checkpoint_occurrence_cannot_be_used_at_two_boundaries():
    first = BalanceCheckpoint(0, Decimal('100'), 'sparse_checkpoint', span(1))
    with pytest.raises(ConstraintViolation, match='control_source_reused'):
        check_checkpoints((Decimal('0'),), (first, replace(first, after=1)))


def test_duplicate_checkpoint_boundaries_do_not_create_financial_links():
    first = BalanceCheckpoint(1, Decimal('100'), 'daily_balance', span(1))
    duplicate = replace(first, source=span(2))
    assert check_checkpoints((Decimal('10'),), (first, duplicate)) == 0
    opening = BalanceCheckpoint(0, Decimal('90'), 'opening_balance', span(3))
    assert check_checkpoints((Decimal('10'),), (opening, first, duplicate)) == 1


def test_checkpoints_from_different_scopes_cannot_reconcile_each_other():
    a = BalanceCheckpoint(0, Decimal('100'), 'sparse_checkpoint', span(1))
    b = BalanceCheckpoint(1, Decimal('110'), 'sparse_checkpoint', span(2, scope='foreign'))
    with pytest.raises(ConstraintViolation, match='control_scope_or_membership'):
        check_checkpoints((Decimal('10'),), (a, b))


def test_duplicate_subtotal_heading_does_not_supply_evidence_for_empty_group(make_document):
    doc = make_document(PERIOD + '01 MAR 2027 Total de créditos +10,00\n'
                        '01 MAR 2027 Total de créditos +10,00\nALFA 10,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID
    assert 'subtotal_reconciliation' in dict(result.pruned_constraints)


def test_all_contradictory_boundary_roles_fail_before_materialization(make_document):
    doc = make_document(PERIOD + 'Saldo: R$ 95,00\n' + MOVEMENTS)
    with patch('pdf_to_ofx.generic.hypotheses.materialize_composition', side_effect=AssertionError('Late rejection')):
        result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID


def test_evidence_strength_cannot_rank_a_plausible_duplicate_opening_against_closing(make_document):
    doc = make_document(PERIOD + 'Saldo inicial: R$ 100,00\nSaldo: R$ 100,00\n01/03/2027 ALFA R$ 0,00')
    result = infer_layout(doc)
    # One interpretation has a closing control. The other has a repeated opening
    # declaration, which provides no additional independent control. Neither is
    # contradicted by the document; a stronger report cannot break the tie.
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.MATERIAL_AMBIGUITY
    assert len(result.hypotheses) == 2
    assert result.interpretation is None


def test_application_preserves_the_typed_failure_diagnostic(write_pdf, tmp_path):
    path = tmp_path / 'ambiguous.pdf'
    write_pdf(PERIOD + 'Saldo: R$ 100,00\n' + MOVEMENTS, path)
    result = analyze_pdf(path)
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.MATERIAL_AMBIGUITY


def test_insufficient_evidence_is_distinct_from_a_material_tie(make_document):
    result = infer_layout(make_document(PERIOD + '01/03/2027 ALFA R$ 10,00'))
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.INSUFFICIENT_EVIDENCE


def test_many_small_domains_do_not_escape_the_global_budget(make_document):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' * 8 + '01/03/2027 ALFA R$ 0,00')
    for reverse in (False, True):
        result = infer_layout(doc, reverse_candidates=reverse)
        assert result.status == AnalysisStatus.AMBIGUOUS
        assert result.diagnostic == InferenceDiagnostic.SEARCH_INCOMPLETE
        assert result.explored_hypotheses == SearchBudget().max_hypotheses == 128
        assert result.interpretation is None


def test_overlapping_movement_spans_cannot_be_counted_twice_by_a_control():
    control = ControlInterval(span(1), (span(2, 0, 2), span(2, 1, 3)), FinancialRole.SUBTOTAL)
    with pytest.raises(ConstraintViolation, match='control_movement_repeated'):
        validate_control_intervals((control,), 'main')


def test_a_movement_is_not_itself_an_independent_control():
    control = ControlInterval(span(1), (span(2),), FinancialRole.MOVEMENT)
    with pytest.raises(ConstraintViolation, match='control_scope_or_membership'):
        validate_control_intervals((control,), 'main')
