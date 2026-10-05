"""Ordered ownership, legitimate nesting and adversarial financial reuse."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus, FinancialRole, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology
from pdf_to_ofx.generic.composition import prepare_composition
from pdf_to_ofx.generic.constraints import constrain_hypothesis
from pdf_to_ofx.generic.hypotheses import StructuralHypothesis
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.amounts import amount_role_candidates
from pdf_to_ofx.generic.operators.dates import date_assignment_candidates
from pdf_to_ofx.generic.operators.directions import infer_direction
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.operators.ownership import control_intervals, movement_sources
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.validation.checkpoints import ConstraintViolation
from pdf_to_ofx.validation.controls import ControlInterval, validate_control_intervals


def span(row, start=0, end=1, scope='main'):
    return SourceSpan(1, row, start, end, region_id=scope)


MOVEMENTS = tuple(span(i) for i in range(10, 14))
OPENING = ControlInterval(span(1), (), FinancialRole.OPENING_BALANCE, 0, 0)
PERIOD = ControlInterval(span(2), MOVEMENTS, FinancialRole.CLOSING_BALANCE, 0, 4, (OPENING.source,))
GROUP = ControlInterval(span(3), MOVEMENTS[:2], FinancialRole.SUBTOTAL, 0, 2)


def validate(*controls, order=MOVEMENTS, scope_sources=None):
    return validate_control_intervals(tuple(controls), 'main', movement_order=order, scope_sources=scope_sources)


def test_period_and_inner_subtotal_are_complementary_controls():
    assert validate(OPENING, PERIOD, GROUP) == 2


def test_explicit_subtotal_hierarchy_accepts_containment():
    outer = replace(PERIOD, role=FinancialRole.SUBTOTAL, references=())
    inner = replace(GROUP, source=span(4), movements=MOVEMENTS[1:2], begin=1, end=2)
    assert validate(outer, GROUP, inner) == 3
    assert validate(inner, GROUP, outer) == 3


def test_set_only_subtotals_cannot_claim_an_unrepresented_hierarchy():
    outer = ControlInterval(span(1), MOVEMENTS, FinancialRole.SUBTOTAL)
    inner = ControlInterval(span(2), MOVEMENTS[:2], FinancialRole.SUBTOTAL)
    with pytest.raises(ConstraintViolation, match='subtotal_interval_overlap'):
        validate_control_intervals((outer, inner), 'main')


def test_equal_economic_intervals_do_not_double_independence():
    duplicate = replace(PERIOD, source=span(4))
    subtotal = replace(PERIOD, source=span(5), role=FinancialRole.SUBTOTAL, references=())
    assert validate(OPENING, PERIOD, duplicate, subtotal) == 1


def test_distinct_copies_of_an_isolated_balance_are_not_financial_links():
    point = ControlInterval(span(1), (), FinancialRole.SPARSE_CHECKPOINT, 2, 2)
    assert validate(point, replace(point, source=span(2))) == 0


def test_balance_endpoints_can_be_shared_as_references_without_being_reowned():
    running = ControlInterval(span(4), MOVEMENTS[:2], FinancialRole.RUNNING_BALANCE, 0, 2, (OPENING.source,))
    later = ControlInterval(span(5), MOVEMENTS[2:], FinancialRole.RUNNING_BALANCE, 2, 4, (running.source,))
    assert validate(OPENING, running, later, PERIOD) == 3


@pytest.mark.parametrize('mutate,constraint', [
    (lambda c: replace(c, source=OPENING.source), 'control_source_reused'),
    (lambda c: replace(c, source=MOVEMENTS[0]), 'circular_financial_control'),
    (lambda c: replace(c, references=(c.source,)), 'circular_financial_control'),
    (lambda c: replace(c, references=(span(9),)), 'control_reference_missing'),
    (lambda c: replace(c, references=(span(1, scope='foreign'),)), 'control_scope_or_membership'),
    (lambda c: replace(c, source=span(2, scope='foreign')), 'control_scope_or_membership'),
    (lambda c: replace(c, begin=-1), 'control_interval_domain'),
    (lambda c: replace(c, end=5), 'control_interval_domain'),
    (lambda c: replace(c, begin=3, end=2), 'control_interval_domain'),
    (lambda c: replace(c, begin=1), 'control_interval_membership'),
    (lambda c: replace(c, movements=MOVEMENTS[:3]), 'control_interval_membership'),
    (lambda c: replace(c, movement_indexes=(0, 0)), 'control_interval_membership'),
])
def test_invalid_control_ownership_fails_closed(mutate, constraint):
    with pytest.raises(ConstraintViolation, match=constraint):
        validate(OPENING, mutate(PERIOD))


def test_circular_reference_chain_is_rejected_explicitly():
    first = ControlInterval(span(1), MOVEMENTS[:2], FinancialRole.SPARSE_CHECKPOINT, 0, 2, (span(2),))
    second = ControlInterval(span(2), MOVEMENTS[2:], FinancialRole.SPARSE_CHECKPOINT, 2, 4, (span(1),))
    with pytest.raises(ConstraintViolation, match='circular_financial_control'):
        validate(first, second)


def test_reference_after_interval_begin_is_economically_impossible():
    point = ControlInterval(span(4), (), FinancialRole.SPARSE_CHECKPOINT, 2, 2)
    with pytest.raises(ConstraintViolation, match='control_reference_order'):
        validate(point, replace(PERIOD, references=(point.source,)))


def test_crossing_subtotal_groups_are_rejected_but_nesting_is_allowed():
    crossing = ControlInterval(span(4), MOVEMENTS[1:3], FinancialRole.SUBTOTAL, 1, 3)
    with pytest.raises(ConstraintViolation, match='subtotal_interval_overlap'):
        validate(GROUP, crossing)


def test_partial_overlap_of_absolute_balance_link_and_subtotal_is_representable():
    # A balance difference can cross a date group. Only impossible group
    # ownership is refused; arbitrary overlap is not a financial contradiction.
    boundary = ControlInterval(span(4), (), FinancialRole.SPARSE_CHECKPOINT, 1, 1)
    checkpoint = ControlInterval(span(5), MOVEMENTS[1:3], FinancialRole.SPARSE_CHECKPOINT, 1, 3, (boundary.source,))
    assert validate(GROUP, boundary, checkpoint) == 2


def test_source_with_correct_scope_id_but_outside_scope_rows_is_rejected():
    scope = tuple(span(i) for i in (1, 2, 10, 11, 12, 13))
    with pytest.raises(ConstraintViolation, match='control_scope_or_membership'):
        validate(OPENING, PERIOD, GROUP, scope_sources=scope)


def test_declared_totals_cover_only_their_signed_movements():
    credits = ControlInterval(span(4), (MOVEMENTS[0], MOVEMENTS[2]), FinancialRole.CREDIT_TOTAL,
                              0, 4, movement_indexes=(0, 2))
    debits = ControlInterval(span(5), (MOVEMENTS[1], MOVEMENTS[3]), FinancialRole.DEBIT_TOTAL,
                             0, 4, movement_indexes=(1, 3))
    assert validate(OPENING, PERIOD, credits, debits) == 3


def test_partial_topology_does_not_supply_evidence_for_unknown_movements():
    order = (None,) * 4
    assert validate(OPENING, replace(PERIOD, movements=()), replace(GROUP, movements=()), order=order) == 0


NESTED = ('Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\nSaldo final: R$ 105,00\n'
          '01 MAR 2027 Total de créditos +10,00\nALFA 6,00\nBETA 4,00\nSaldo intermediário: R$ 110,00\n'
          '02 MAR 2027 Total de débitos -5,00\nGAMA 2,00\nDELTA 3,00')


def prepare(document):
    prepared = prepare_composition(document, observe_geometry(document, None, Tolerances()), legacy_context=False)
    coverage = VisualCoverage(prepared.original, prepared.profile.tolerances)
    dates = tuple(domain[0] for domain in date_assignment_candidates(prepared.segments, prepared.candidates))
    amounts = tuple(amount_role_candidates(s, prepared.profile)[0] for s in prepared.segments)
    directions = tuple(infer_direction(s, roles, prepared.profile.amount_mode, prepared.candidates,
        prepared.profile.tolerances, carry_across_pages=prepared.profile.carry_date_across_pages)
        for s, roles in zip(prepared.segments, amounts, strict=True))
    hypothesis = StructuralHypothesis(prepared.scope, prepared.segments, dates, amounts, directions, Chronology.ASCENDING,
                                      monetary_domains=prepared.monetary_domains)
    return prepared, coverage, hypothesis


def test_operator_topology_nests_period_with_groups_and_checkpoint(make_document):
    doc = make_document(NESTED)
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.SUCCESS
    assert result.interpretation.evidence.group_subtotals_verified == EvidenceStatus.VERIFIED
    assert result.interpretation.evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    prepared, coverage, hypothesis = prepare(doc)
    points = constrain_hypothesis(prepared, hypothesis)
    controls = control_intervals(prepared, hypothesis, coverage, points,
        tuple(d.apply(a.magnitude) for d, a in zip(hypothesis.directions, hypothesis.monetary_roles, strict=True)))
    assert {(c.begin, c.end) for c in controls} == {(0, 0), (0, 2), (2, 4), (0, 4)}
    assert validate_control_intervals(controls, 'main', movement_order=movement_sources(prepared, hypothesis, coverage)) == 3


def test_invalid_control_scope_prunes_a_root_before_materialization(make_document):
    doc = make_document(NESTED)
    foreign = BalanceCheckpoint(0, Decimal('100'), 'opening_balance', span(1, scope='foreign'))
    with patch('pdf_to_ofx.generic.constraints.checkpoint_candidates', return_value=(foreign,)), \
         patch('pdf_to_ofx.generic.hypotheses.materialize_composition', side_effect=AssertionError('Late check')):
        result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID
    assert result.candidate_hypotheses == 0
    assert dict(result.pruned_constraints)['control_scope_or_membership'] > 0


def test_empty_understood_subtotal_group_is_pruned_before_transactions(make_document):
    text = NESTED.replace('ALFA 6,00\nBETA 4,00\nSaldo intermediário: R$ 110,00\n', '')
    result = infer_layout(make_document(text))
    assert result.status == AnalysisStatus.INVALID
    assert result.candidate_hypotheses == 0
    assert 'subtotal_reconciliation' in dict(result.pruned_constraints)
