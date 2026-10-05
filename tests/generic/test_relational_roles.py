"""Structural monetary authorization, using only fictitious positioned cells."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf, export_analysis
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus, FinancialRole, InferenceDiagnostic
from pdf_to_ofx.domain.models import BankAccount
from pdf_to_ofx.generic.composition import prepare_composition
from pdf_to_ofx.generic.hypotheses import SearchBudget
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.amounts import EmptyDomainCause
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.operators import relations
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.ofx.generator import account_profile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens

RECIPE = Path(__file__).parents[1] / 'fixtures/layouts/relational_balances/pages.json'
ACCOUNT = BankAccount('Instituição Fictícia', '999', '999', '0001', 'DEMO-0001')


@pytest.fixture
def relational_document(write_layout_pdf, tmp_path):
    write_layout_pdf(RECIPE, tmp_path / 'relational.pdf')
    return extract_pdf(tmp_path / 'relational.pdf')


def modified_document(write_layout_pdf, tmp_path, change):
    recipe = json.loads(RECIPE.read_text())
    change(recipe['pages'][0])
    path = tmp_path / 'modified.json'
    path.write_text(json.dumps(recipe))
    write_layout_pdf(path, tmp_path / 'modified.pdf')
    return extract_pdf(tmp_path / 'modified.pdf')


def test_unlabelled_cells_have_observations_before_role_assignment(relational_document):
    doc = relational_document
    prepared = prepare_composition(doc, observe_geometry(doc, None, Tolerances()), legacy_context=False)
    assert len(prepared.monetary_observations) == 9
    assert len(prepared.monetary_domains) == 3
    assert prepared.context.opening_balance is prepared.context.closing_balance is None
    assert not prepared.declarations
    assert all(d.column == 1 and d.decisions for d in prepared.monetary_domains)
    assert [d.boundary for d in prepared.monetary_domains] == [0, 2, 3]
    assert prepared.monetary_domains[1].roles == (FinancialRole.SPARSE_CHECKPOINT,)
    for domain in prepared.monetary_domains:
        assert source_tokens(doc, domain.source)
        for decision in domain.decisions:
            assert decision.witnesses
            assert all(source_tokens(doc, source) for source in decision.witnesses)


def test_frontiers_and_group_cut_authorize_balances_without_a_caption(relational_document):
    result = infer_layout(relational_document)
    assert result.status == AnalysisStatus.SUCCESS
    statement, evidence = result.interpretation.statement, result.interpretation.evidence
    assert statement.opening_balance == Decimal('100')
    assert statement.closing_balance == Decimal('109')
    assert [t.amount for t in statement.transactions] == [Decimal('10'), Decimal('-3'), Decimal('2')]
    assert [(p.after, p.balance) for p in statement.checkpoints] == [(2, Decimal('107'))]
    assert evidence.has_financial_support
    assert evidence.financial_coverage_verified == EvidenceStatus.VERIFIED
    assert all(role.role_evidence for h in result.hypotheses for role in h.monetary_roles)
    assert 'control_economic_frontier' in dict(result.pruned_constraints)
    assert 'control_column_ownership' in dict(result.pruned_constraints)
    assert result.explored_hypotheses <= 128


@pytest.mark.parametrize('reverse', [False, True])
def test_relational_generators_are_order_independent(relational_document, reverse):
    original = infer_layout(relational_document)
    with patch.object(relations, 'RELATIONAL_GENERATORS', relations.RELATIONAL_GENERATORS[::-1]):
        changed = infer_layout(relational_document, reverse_candidates=reverse)
    assert changed.status == original.status == AnalysisStatus.SUCCESS
    assert changed.interpretation == original.interpretation
    assert changed.profile == original.profile
    assert changed.pruned_constraints == original.pruned_constraints
    assert generate_ofx(changed.interpretation.statement, account_profile(ACCOUNT)) == generate_ofx(
        original.interpretation.statement, account_profile(ACCOUNT))


def test_relational_domains_depend_on_geometry_not_on_amounts(relational_document):
    original = prepare_composition(relational_document, observe_geometry(relational_document, None, Tolerances()), legacy_context=False)
    changed = replace(relational_document, pages=tuple(replace(p, words=tuple(
        replace(w, text='999,99') if ',' in w.text else w for w in p.words)) for p in relational_document.pages))
    other = prepare_composition(changed, observe_geometry(changed, None, Tolerances()), legacy_context=False)
    assert [(d.source, d.roles, d.boundary, d.column, d.decisions) for d in original.monetary_domains] == [
        (d.source, d.roles, d.boundary, d.column, d.decisions) for d in other.monetary_domains]
    assert infer_layout(changed).status == AnalysisStatus.INVALID


@pytest.mark.parametrize('text,cause', [
    ('R$ 100,00', EmptyDomainCause.NO_STRUCTURAL_OWNER),
    ('VALOR DESCONHECIDO R$ 100,00', EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT),
    ('Subtotal: R$ 100,00', EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE),
])
def test_empty_domains_explain_structural_refusal(make_document, text, cause):
    doc = make_document('Período: 01/03/2027 a 03/03/2027\n' + text +
                        '\n01/03/2027 ALFA +R$ 10,00\n02/03/2027 BETA -R$ 10,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('monetary_role_domain_empty',)
    assert result.diagnostic == InferenceDiagnostic.CAPABILITY_MISSING
    assert result.candidate_hypotheses == 0
    assert len(result.monetary_diagnostics) == 1
    diagnostic = result.monetary_diagnostics[0]
    assert diagnostic.cause == cause
    assert {d.generator for d in diagnostic.decisions} == {'scope_boundary', 'group_boundary'}
    assert not any(d.admitted for d in diagnostic.decisions)


def test_a_reconciling_sequence_without_transaction_descriptions_is_unsupported(make_document):
    text = ('Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\nSaldo final: R$ 107,00\n'
            '01/03/2027 +R$ 10,00 R$ 110,00\n02/03/2027 -R$ 3,00 R$ 107,00')
    result = infer_layout(make_document(text))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.interpretation is None


def test_a_balance_column_without_group_frontier_cannot_create_a_checkpoint(write_layout_pdf, tmp_path):
    def change(rows):
        rows.pop(6)
    result = infer_layout(modified_document(write_layout_pdf, tmp_path, change))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('monetary_role_domain_empty',)
    assert result.monetary_diagnostics[0].cause == EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE


def test_a_nonrecurring_column_cannot_authorize_unlabelled_balances(write_layout_pdf, tmp_path):
    def change(rows):
        rows[7]['cells'][-1][0] += 30
    result = infer_layout(modified_document(write_layout_pdf, tmp_path, change))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert all(d.cause == EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT for d in result.monetary_diagnostics)


def test_foreign_caption_cannot_borrow_a_balance_column(write_layout_pdf, tmp_path):
    def change(rows):
        rows[1]['cells'].insert(0, [30, 'CONTROLE NÃO DEFINIDO'])
    result = infer_layout(modified_document(write_layout_pdf, tmp_path, change))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.monetary_diagnostics[0].cause == EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT


def test_unlabelled_control_column_is_a_constraint_not_a_math_hint(write_layout_pdf, tmp_path):
    def change(rows):
        # Opening/closing can mathematically reconcile the right-column
        # interpretation, but cells aligned with the other column forbid it.
        for index in (1, 5, 8):
            rows[index]['cells'][0][0] = 330
    result = infer_layout(modified_document(write_layout_pdf, tmp_path, change))
    assert result.status == AnalysisStatus.INVALID
    assert 'control_column_ownership' in dict(result.pruned_constraints)
    assert not result.hypotheses


@pytest.mark.parametrize('budget', [SearchBudget(max_local_alternatives=1), SearchBudget(max_hypotheses=3)])
def test_relational_domains_do_not_expand_search_budget(relational_document, budget):
    for reverse in (False, True):
        result = infer_layout(relational_document, budget=budget, reverse_candidates=reverse)
        assert result.status == AnalysisStatus.AMBIGUOUS
        assert result.diagnostic == InferenceDiagnostic.SEARCH_INCOMPLETE
        assert result.interpretation is None
        assert result.explored_hypotheses <= budget.max_hypotheses


def test_application_exports_actual_relational_pdf_deterministically(write_layout_pdf, tmp_path):
    path = tmp_path / 'relational.pdf'
    write_layout_pdf(RECIPE, path)
    first, second = analyze_pdf(path), analyze_pdf(path)
    assert first.status == second.status == AnalysisStatus.SUCCESS
    assert first == second
    assert export_analysis(first, ACCOUNT).encode('cp1252') == export_analysis(second, ACCOUNT).encode('cp1252')


def test_new_inference_does_not_call_legacy_frame_observation(relational_document):
    with patch('pdf_to_ofx.generic.grouped.infer_grouped_profile', side_effect=AssertionError('Legacy geometry')):
        assert infer_layout(relational_document).status == AnalysisStatus.SUCCESS
