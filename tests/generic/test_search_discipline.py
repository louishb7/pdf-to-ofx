"""Causal index-only traces, early pruning and metamorphic search stability."""

from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.domain.evidence import AnalysisStatus, FinancialRole, InferenceDiagnostic, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, BankAccount
from pdf_to_ofx.generic.composition import prepare_composition
from pdf_to_ofx.generic.constraints import observe_constraint_facts
from pdf_to_ofx.generic.hypotheses import SearchBudget
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators import relations, transactions
from pdf_to_ofx.generic.operators.amounts import EmptyDomainCause
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.ofx.generator import account_profile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens
from pdf_to_ofx.validation.checkpoints import ConstraintViolation
from pdf_to_ofx.validation.controls import ControlInterval, validate_control_intervals

PERIOD = 'Período: 01/03/2027 a 03/03/2027\n'
HEADER = PERIOD + 'Saldo inicial: R$ 100,00\nSaldo final: R$ 109,00\n'
BODY = ('01/03/2027 ALFA +R$ 10,00 R$ 110,00\n'
        '02/03/2027 BETA -R$ 3,00 R$ 107,00\n03/03/2027 GAMA +R$ 2,00 R$ 109,00')
ACCOUNT = BankAccount('Instituição Fictícia', '999', '999', '0001', 'DEMO-0001')
RECIPE = Path(__file__).parents[1] / 'fixtures/layouts/relational_balances/pages.json'


@pytest.fixture
def relational_document(write_layout_pdf, tmp_path):
    path = tmp_path / 'relational.pdf'
    write_layout_pdf(RECIPE, path)
    return extract_pdf(path)


def test_trace_reconstructs_parent_choices_and_structural_authorizations(relational_document):
    result = infer_layout(relational_document)
    assert result.status == AnalysisStatus.SUCCESS
    assert len(result.search_trace) == result.explored_hypotheses <= 128
    decisions = {(d.source, d.role) for d in result.role_decisions if d.admitted}
    for step in result.search_trace:
        assert step.state < 128
        assert step.parent is None or step.parent < step.state
        assert all((a.source, a.role) in decisions for a in step.choices)
        assert step.outcome in {'expanded', 'pruned', 'survived'}
        assert (step.constraint is not None) == (step.outcome == 'pruned')
        if step.date_source:
            assert source_tokens(relational_document, step.date_source)
    for survivor in (step for step in result.search_trace if step.outcome == 'survived'):
        chain = []
        step = survivor
        while True:
            chain.extend(step.choices)
            if step.parent is None:
                break
            step = result.search_trace[step.parent]
        assert len(chain) == len({a.source for a in chain}) == 9
        assert survivor.complete and survivor.controls
        assert all((c.source, c.role) in decisions for c in survivor.controls)


def test_trace_records_financial_death_without_copying_financial_contents(make_document):
    result = infer_layout(make_document(HEADER.replace('109,00', '999,00') + BODY))
    assert result.status == AnalysisStatus.INVALID
    assert result.search_trace
    assert any(step.constraint == 'checkpoint_same_boundary' for step in result.search_trace)
    trace = json.dumps([asdict(s) for s in result.search_trace], default=str)
    assert all(text not in trace for text in ('ALFA', 'BETA', 'GAMA', 'R$', '999,00', '110,00'))
    assert not any(step.outcome == 'survived' for step in result.search_trace)


def test_fixed_chronology_contradiction_dies_at_root(make_document):
    result = infer_layout(make_document(HEADER + BODY))
    assert result.status == AnalysisStatus.SUCCESS
    chronology = [s for s in result.search_trace if s.constraint == 'chronology_dates']
    assert len(chronology) == 1
    assert chronology[0].parent is None and not chronology[0].choices and not chronology[0].complete


def test_structural_facts_are_observed_once_and_remain_unclaimed(make_document):
    captured = []

    def observe(prepared):
        facts = observe_constraint_facts(prepared)
        captured.append(facts)
        return facts

    with patch('pdf_to_ofx.generic.hypotheses.observe_constraint_facts', side_effect=observe) as observed:
        result = infer_layout(make_document(HEADER + BODY))
    assert result.status == AnalysisStatus.SUCCESS
    assert observed.call_count == 1
    facts = captured[0]
    assert facts.coverage.assignments == {} and facts.coverage.transactions == []


def test_date_nearness_without_description_never_expands_a_hypothesis(make_document):
    result = infer_layout(make_document(HEADER + '01/03/2027 +R$ 9,00 R$ 109,00'))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.candidate_hypotheses == result.explored_hypotheses == 0
    assert not result.search_trace
    assert result.monetary_diagnostics
    assert all(d.cause == EmptyDomainCause.UNSUPPORTED_TRANSACTION_GEOMETRY for d in result.monetary_diagnostics)
    assert not any(d.admitted for m in result.monetary_diagnostics for d in m.decisions)


def test_two_material_survivors_are_not_scored_by_control_strength(make_document):
    doc = make_document(PERIOD + 'Saldo inicial: R$ 100,00\nSaldo: R$ 100,00\n01/03/2027 ALFA R$ 0,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.MATERIAL_AMBIGUITY
    survivors = [s for s in result.search_trace if s.outcome == 'survived']
    assert len(survivors) == len(result.hypotheses) == 2
    assert {bool(any(c.movements for c in s.controls)) for s in survivors} == {False, True}


def test_unexplored_material_alternative_blocks_an_already_visited_survivor(make_document):
    doc = make_document(PERIOD + 'Saldo inicial: R$ 100,00\nSaldo final: R$ 110,00\n'
                        'ALFA 01/03/2027 02/03/2027 R$ 10,00')
    result = infer_layout(doc, budget=SearchBudget(max_hypotheses=2))
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.SEARCH_INCOMPLETE
    assert result.interpretation is None
    assert len(result.search_trace) == 2
    assert result.search_trace[-1].outcome == 'survived'


@pytest.mark.parametrize('count', [7, 8, 9])
@pytest.mark.parametrize('reverse', [False, True])
def test_many_small_domains_keep_trace_and_search_inside_128_states(make_document, count, reverse):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' * count + '01/03/2027 ALFA R$ 0,00')
    result = infer_layout(doc, reverse_candidates=reverse)
    assert result.status == AnalysisStatus.AMBIGUOUS
    assert result.diagnostic == InferenceDiagnostic.SEARCH_INCOMPLETE
    assert len(result.search_trace) == result.explored_hypotheses == 128
    assert result.interpretation is None


def test_unassigned_control_cannot_reserve_a_transaction_source(make_document):
    doc = make_document(PERIOD + 'Saldo: R$ 100,00\n' + BODY)
    prepared = prepare_composition(doc, observe_geometry(doc, None, Tolerances()), legacy_context=False)
    coverage = VisualCoverage(prepared.original, prepared.profile.tolerances)
    first = prepared.segments[0].rows[0]
    source = coverage.span(first.row, first.amounts[0].start, first.amounts[0].end)
    domain = prepared.monetary_domains[0]
    domain = replace(domain, source=source, decisions=tuple(replace(d, source=source) for d in domain.decisions))
    prepared = replace(prepared, monetary_domains=(domain,))
    with patch('pdf_to_ofx.generic.hypotheses.prepare_composition', return_value=prepared), \
         patch('pdf_to_ofx.generic.hypotheses.materialize_composition', side_effect=AssertionError('Late ownership check')):
        result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID
    assert result.candidate_hypotheses == 0
    assert all(s.constraint == 'exclusive_monetary_ownership' and not s.choices for s in result.search_trace)


def test_overlapping_column_relations_have_a_specific_empty_domain_cause(relational_document):
    rows = reconstruct_rows(relational_document)
    control_words = rows[1].words
    right_edge = rows[3].words[-1].x1
    changed = replace(relational_document, pages=tuple(replace(p, words=tuple(
        replace(w, x0=330, x1=340) if w is control_words[0] else
        replace(w, x0=343, x1=right_edge) if w is control_words[1] else w for w in p.words))
        for p in relational_document.pages))
    result = infer_layout(changed)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.monetary_diagnostics[0].cause == EmptyDomainCause.CONFLICTING_STRUCTURAL_EVIDENCE


def test_declared_checkpoint_outside_transaction_area_has_no_representable_cut(make_document):
    result = infer_layout(make_document(PERIOD + 'Saldo intermediário: R$ 100,00\n' + BODY))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.monetary_diagnostics[0].cause == EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE


@pytest.mark.parametrize('dx,dy', [(20, 30), (-5, 7)])
def test_nonmaterial_translation_preserves_interpretation_and_sources(relational_document, dx, dy):
    changed = replace(relational_document, pages=tuple(replace(p, words=tuple(
        replace(w, x0=w.x0 + dx, x1=w.x1 + dx, top=w.top + dy, bottom=w.bottom + dy) for w in p.words))
        for p in relational_document.pages))
    original, other = infer_layout(relational_document), infer_layout(changed, reverse_candidates=True)
    assert original.status == other.status == AnalysisStatus.SUCCESS
    assert original.interpretation == other.interpretation
    assert original.role_decisions == other.role_decisions
    assert original.blocking_capabilities == other.blocking_capabilities
    assert original.diagnostic == other.diagnostic
    assert generate_ofx(original.interpretation.statement, account_profile(ACCOUNT)) == generate_ofx(
        other.interpretation.statement, account_profile(ACCOUNT))


def test_equivalent_description_constructions_have_canonical_provenance(make_document):
    doc = make_document(HEADER + BODY.replace('ALFA', 'OPERAÇÃO ALFA'))
    original = infer_layout(doc)
    describe = transactions.description_intervals

    def split(*args, **kwargs):
        output = []
        for row, begin, end in describe(*args, **kwargs):
            output.extend(((row, begin, begin + 1), (row, begin + 1, end)) if end - begin > 1 else ((row, begin, end),))
        return tuple(output)

    with patch('pdf_to_ofx.generic.operators.local.description_intervals', side_effect=split), \
         patch('pdf_to_ofx.generic.composition.description_intervals', side_effect=split), \
         patch('pdf_to_ofx.generic.constraints.description_intervals', side_effect=split):
        changed = infer_layout(doc, reverse_candidates=True)
    assert original.status == changed.status == AnalysisStatus.SUCCESS
    assert original.interpretation == changed.interpretation
    assert original.role_decisions == changed.role_decisions
    assert generate_ofx(original.interpretation.statement, account_profile(ACCOUNT)).encode('cp1252') == generate_ofx(
        changed.interpretation.statement, account_profile(ACCOUNT)).encode('cp1252')


def test_generator_inversion_preserves_empty_domain_diagnostic(make_document):
    doc = make_document(PERIOD + 'VALOR DESCONHECIDO: R$ 100,00\n' + BODY)
    original = infer_layout(doc)
    with patch.object(relations, 'RELATIONAL_GENERATORS', relations.RELATIONAL_GENERATORS[::-1]):
        changed = infer_layout(doc, reverse_candidates=True)
    assert original.status == changed.status == AnalysisStatus.UNSUPPORTED
    assert original.monetary_diagnostics == changed.monetary_diagnostics
    assert original.blocking_capabilities == changed.blocking_capabilities
    assert original.diagnostic == changed.diagnostic


def test_control_reference_must_anchor_the_exact_economic_begin():
    opening, movement, later = (SourceSpan(1, row, 0, 1) for row in (1, 2, 3))
    anchor = ControlInterval(opening, (), FinancialRole.SPARSE_CHECKPOINT, 0, 0)
    control = ControlInterval(later, (movement,), FinancialRole.SPARSE_CHECKPOINT, 1, 2, (anchor.source,))
    with pytest.raises(ConstraintViolation, match='control_reference_order'):
        validate_control_intervals((anchor, control), 'main', movement_order=(None, movement))


def test_duplicate_endpoint_references_cannot_manufacture_independence():
    opening, movement, closing = (SourceSpan(1, row, 0, 1) for row in (1, 2, 3))
    anchor = ControlInterval(opening, (), FinancialRole.OPENING_BALANCE, 0, 0)
    control = ControlInterval(closing, (movement,), FinancialRole.CLOSING_BALANCE, 0, 1, (opening, opening))
    with pytest.raises(ConstraintViolation, match='control_source_reused'):
        validate_control_intervals((anchor, control), 'main', movement_order=(movement,))


@pytest.mark.parametrize('role,begin,end', [(FinancialRole.OPENING_BALANCE, 1, 1),
    (FinancialRole.CLOSING_BALANCE, 1, 1), (FinancialRole.CREDIT_TOTAL, 1, 2)])
def test_scope_roles_cannot_reanchor_to_an_internal_interval(role, begin, end):
    source, first, second = (SourceSpan(1, row, 0, 1) for row in (1, 2, 3))
    members = () if begin == end else (second,)
    control = ControlInterval(source, members, role, begin, end)
    with pytest.raises(ConstraintViolation, match='control_interval_domain'):
        validate_control_intervals((control,), 'main', movement_order=(first, second))


@pytest.mark.parametrize('caption', ['Saldo:', 'Subtotal:'])
@pytest.mark.parametrize('heading', ['01/03/2027', '01 MAR 2027 Total de créditos +10,00'])
def test_control_caption_in_a_date_group_cannot_be_invented_as_a_zero_movement(make_document, caption, heading):
    movement = 'ALFA 10,00' if 'Total' in heading else 'ALFA +R$ 10,00'
    doc = make_document(PERIOD + 'Saldo inicial: R$ 100,00\nSaldo final: R$ 110,00\n'
                        + heading + '\n' + movement + '\n' + caption + ' R$ 0,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('monetary_role_domain_empty',)
    assert result.candidate_hypotheses == 0
    assert result.monetary_diagnostics[0].cause == EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE


def test_crossing_group_intervals_are_invalid_while_movements_are_unassigned():
    first = ControlInterval(SourceSpan(1, 1, 0, 1), (), FinancialRole.SUBTOTAL, 0, 2)
    second = ControlInterval(SourceSpan(1, 2, 0, 1), (), FinancialRole.SUBTOTAL, 1, 3)
    with pytest.raises(ConstraintViolation, match='subtotal_interval_overlap'):
        validate_control_intervals((first, second), 'main', movement_order=(None,) * 4)


def test_checkpoint_requires_an_assigned_monetary_role_before_materialization(make_document):
    doc = make_document(HEADER + BODY)
    # The period word belongs to the scope, but cannot become a financial source.
    point = BalanceCheckpoint(0, Decimal('100'), 'sparse_checkpoint', SourceSpan(1, 1, 0, 1))
    with patch('pdf_to_ofx.generic.constraints.checkpoint_candidates', return_value=(point,)), \
         patch('pdf_to_ofx.generic.hypotheses.materialize_composition', side_effect=AssertionError('Late origin check')):
        result = infer_layout(doc)
    assert result.status == AnalysisStatus.INVALID
    assert result.candidate_hypotheses == 0
    assert 'control_origin' in dict(result.pruned_constraints)


@pytest.mark.parametrize('limit', [1, 4])
def test_incomplete_segments_do_not_spend_the_local_budget(make_document, limit):
    result = infer_layout(make_document(HEADER + '01/03/2027 +R$ 9,00 R$ 109,00'),
                          budget=SearchBudget(max_local_alternatives=limit))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.diagnostic == InferenceDiagnostic.CAPABILITY_MISSING
    assert not result.budget_exhausted
    assert result.explored_hypotheses == 0


def test_two_nearby_dates_are_not_a_transaction_description(make_document):
    doc = make_document(HEADER + '01/03/2027 02/03/2027 +R$ 9,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.candidate_hypotheses == result.explored_hypotheses == 0
    assert result.blocking_capabilities == ('transaction_segmentation',)
