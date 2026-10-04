"""Small hypothesis domains, hard pruning and material determinism (fictitious)."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf, assess_export_readiness, ExportStatus, export_analysis
from pdf_to_ofx.banks.inter import InterParser
from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.domain.errors import AmbiguousStatementError, FinancialCoverageError, StatementValidationError
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus, FinancialRole, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, BankAccount, Chronology
from pdf_to_ofx.generic.composition import material_key, prepare_composition, resolve_hypotheses
from pdf_to_ofx.generic.hypotheses import SearchBudget, StructuralHypothesis, constrain_hypothesis
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.amounts import amount_role_candidates
from pdf_to_ofx.generic.operators.dates import date_assignment_candidates
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.ofx.generator import generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens
from pdf_to_ofx.validation.checkpoints import ConstraintViolation, check_checkpoints
from pdf_to_ofx.validation.coverage import CoverageLedger
from pdf_to_ofx.validation.statement import validate_statement

LAYOUTS = Path(__file__).parents[1] / 'fixtures/layouts'
ACCOUNT = BankAccount('Instituição Fictícia', '999', '999', '0001', 'DEMO-0001')
HEADER = 'Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\nSaldo final: R$ 110,00\n'


@pytest.fixture(params=['descending_checkpoints', 'daily_checkpoints', 'sparse_checkpoints'])
def checkpoint_pdf(request, write_layout_pdf, tmp_path):
    target = tmp_path / f'{request.param}.pdf'
    write_layout_pdf(LAYOUTS / request.param / 'pages.json', target)
    return request.param, target


def test_checkpoint_recipes_through_actual_pdf_and_application(checkpoint_pdf):
    name, path = checkpoint_pdf
    document = extract_pdf(path)
    result = infer_layout(document)
    assert result.status == AnalysisStatus.SUCCESS
    statement, evidence = result.interpretation.statement, result.interpretation.evidence
    assert evidence.financial_coverage_verified == EvidenceStatus.VERIFIED
    assert result.hypotheses and all(h.complete for h in result.hypotheses)
    assert statement.chronology == (Chronology.DESCENDING if name.startswith('descending') else Chronology.ASCENDING)
    if name.startswith('descending'):
        # Dates are identical: only checkpoint mathematics can decide direction.
        assert len({t.posting_date for t in statement.transactions}) == 1
        assert [t.amount for t in statement.transactions] == [Decimal('2'), Decimal('-5'), Decimal('10')]
        assert result.interpretation.provenance.chronology.economic_order == (2, 1, 0)
        assert evidence.running_balance_links == 3
    else:
        assert all(t.balance_after is None for t in statement.transactions)
        assert statement.checkpoints and evidence.checkpoint_links == 2
        assert evidence.running_balance_links == 0
        assert evidence.checkpoints_verified == EvidenceStatus.VERIFIED
        if name.startswith('daily'):
            assert evidence.daily_balances_verified == EvidenceStatus.VERIFIED
    analysis = analyze_pdf(path)
    assert analysis.statement == statement and analysis.status == AnalysisStatus.SUCCESS
    assert assess_export_readiness(analysis).status == ExportStatus.EXPORT_METADATA_REQUIRED
    payload = export_analysis(analysis, ACCOUNT)
    assert payload == export_analysis(analysis, ACCOUNT)
    assert payload == generate_ofx(statement, metadata_profile())
    for transaction, source in zip(statement.transactions, analysis.provenance.transactions, strict=True):
        tokens = lambda span: source_tokens(document, span, result.profile.tolerances)
        assert ' '.join(t for s in source.description_sources for t in tokens(s)) == transaction.description
        assert tokens(source.date_source) and tokens(source.amount_source) and tokens(source.direction_source)
        if transaction.balance_after is not None:
            assert tokens(source.balance_source)
    for checkpoint in statement.checkpoints:
        assert source_tokens(document, checkpoint.source)
        assert any(a.source == checkpoint.source and a.role.value == checkpoint.kind
                   for a in analysis.provenance.monetary_regions)


def metadata_profile():
    from pdf_to_ofx.ofx.generator import account_profile
    return account_profile(ACCOUNT)


def test_local_dates_and_roles_remain_candidates_before_decision(make_document):
    doc = make_document(HEADER + 'OPERAÇÃO 28/02/2027 01/03/2027 R$ 10,00 R$ 110,00')
    geometry = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    prepared = prepare_composition(doc, geometry, legacy_context=False)
    dates = date_assignment_candidates(prepared.segments, prepared.candidates)
    roles = amount_role_candidates(prepared.segments[0], geometry)
    assert len(dates[0]) == len(roles) == 2
    assert {d.candidate.value for d in dates[0]} == {date(2027, 2, 28), date(2027, 3, 1)}
    assert roles[0].movement == roles[1].running_balance


def test_closed_totals_do_not_override_temporal_origin_constraint(make_document):
    # Both date assignments reconcile opening/closing; only one is in the period.
    doc = make_document(HEADER + 'OPERAÇÃO 28/02/2027 01/03/2027 R$ 10,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.SUCCESS
    assert result.interpretation.statement.transactions[0].posting_date == date(2027, 3, 1)
    assert dict(result.pruned_constraints)['date_period'] == 1
    assert len(result.hypotheses) == 1


def test_closed_totals_with_two_material_dates_are_really_ambiguous(make_document):
    doc = make_document(HEADER + 'OPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00')
    result = infer_layout(doc)
    assert result.status == AnalysisStatus.AMBIGUOUS and result.interpretation is None
    assert result.candidate_hypotheses == len(result.hypotheses) == 2
    assert len({h.semantic_key() for h in result.hypotheses}) == 2
    with pytest.raises(FrozenInstanceError):
        result.hypotheses[0].chronology = Chronology.UNKNOWN


@pytest.mark.parametrize('opening,closing', [('100,00', '110,00'), ('100,00', '111,00')])
def test_financial_pruning_precedes_statement_materialization(make_document, opening, closing):
    doc = make_document(HEADER.replace('110,00', closing) + '01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    from pdf_to_ofx.generic.hypotheses import materialize_composition
    with patch('pdf_to_ofx.generic.hypotheses.materialize_composition', wraps=materialize_composition) as build:
        result = infer_layout(doc)
    assert build.call_count == (2 if closing == '110,00' else 0) # Equivalent one-movement chronology hypotheses.
    assert result.status == (AnalysisStatus.SUCCESS if closing == '110,00' else AnalysisStatus.INVALID)
    assert result.pruned_constraints


def test_subtotal_partial_bound_prunes_before_full_group(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    # A wrong movement magnitude exceeds its group's independent subtotal early.
    changed = replace(document, pages=tuple(replace(page, words=tuple(
        replace(word, text='99,00') if word.text == '10,00' else word for word in page.words))
        for page in document.pages))
    result = infer_layout(changed)
    assert result.status == AnalysisStatus.INVALID
    assert dict(result.pruned_constraints)['subtotal_partial_bound'] > 0
    assert result.candidate_hypotheses == 0


@pytest.mark.parametrize('limit', [1, 2, 3, 4])
def test_global_budget_never_returns_a_partial_winner(make_document, limit):
    doc = make_document(HEADER + 'OPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00')
    result = infer_layout(doc, budget=SearchBudget(max_hypotheses=limit))
    if limit < 3:
        assert result.status == AnalysisStatus.AMBIGUOUS and result.budget_exhausted
        assert result.blocking_capabilities == ('search_budget',)
        assert result.interpretation is None
    else:
        assert result.status == AnalysisStatus.AMBIGUOUS and not result.budget_exhausted


def test_local_budget_does_not_select_the_first_date(make_document):
    result = infer_layout(make_document(HEADER + 'OPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00'),
                          budget=SearchBudget(max_local_alternatives=1))
    assert result.status == AnalysisStatus.AMBIGUOUS and result.budget_exhausted
    assert result.explored_hypotheses == 0


@pytest.mark.parametrize('changes', [{'max_local_alternatives':0}, {'max_hypotheses':False}, {'max_hypotheses':1.5}])
def test_budget_requires_small_positive_integer_limits(changes):
    with pytest.raises(ValueError):
        SearchBudget(**changes)


@pytest.mark.parametrize('kind', ['daily_balance', 'sparse_checkpoint'])
def test_partial_checkpoint_link_is_checked_as_soon_as_complete(kind):
    points = (BalanceCheckpoint(0, Decimal('100'), 'opening_balance'), BalanceCheckpoint(2, Decimal('107'), kind))
    assert check_checkpoints((Decimal('10'), None, None), points) == 0
    assert check_checkpoints((Decimal('10'), Decimal('-3'), None), points) == 1
    with pytest.raises(ConstraintViolation, match='checkpoint_reconciliation'):
        check_checkpoints((Decimal('10'), Decimal('-2'), None), points)


@pytest.mark.parametrize('point', [BalanceCheckpoint(-1, Decimal('1'), 'daily_balance'),
                                   BalanceCheckpoint(2, Decimal('1'), 'daily_balance'),
                                   BalanceCheckpoint(1, Decimal('NaN'), 'daily_balance')])
def test_invalid_checkpoint_domains_are_rejected(point):
    with pytest.raises(ConstraintViolation, match='checkpoint_domain'):
        check_checkpoints((Decimal('1'),), (point,))


def test_coincident_checkpoint_disagreement_is_rejected_without_movements():
    with pytest.raises(ConstraintViolation, match='checkpoint_same_boundary'):
        check_checkpoints((None,), (BalanceCheckpoint(0, Decimal('100'), 'opening_balance'),
                                   BalanceCheckpoint(0, Decimal('101'), 'sparse_checkpoint')))


def test_checkpoint_math_independent_of_callers_decimal_precision(checkpoint_pdf):
    with localcontext() as context:
        context.prec = 2
        assert infer_layout(extract_pdf(checkpoint_pdf[1])).status == AnalysisStatus.SUCCESS


def test_absent_controls_are_unavailable_and_cannot_authorize_success(make_document):
    result = infer_layout(make_document('Período: 01/03/2027 a 03/03/2027\n01/03/2027 OPERAÇÃO R$ 10,00'))
    assert result.status == AnalysisStatus.AMBIGUOUS and len(result.hypotheses) == 1
    assert not result.budget_exhausted


def test_missing_structure_diagnostic_is_specific(make_document):
    result = infer_layout(make_document('DECLARAÇÃO FICTÍCIA\n01/03/2027 OPERAÇÃO R$ 10,00'))
    assert result.status == AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities == ('period_declaration_missing',)


def test_supplied_profile_reduces_roles_but_cannot_bypass_math(make_document):
    doc = make_document(HEADER + '01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    good = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    bad = replace(good, movement_column=1, balance_column=0)
    unconstrained, constrained, contradicted = infer_layout(doc), infer_layout(doc, profile=good), infer_layout(doc, profile=bad)
    assert unconstrained.status == constrained.status == AnalysisStatus.SUCCESS
    assert constrained.explored_hypotheses < unconstrained.explored_hypotheses
    assert contradicted.status == AnalysisStatus.INVALID
    assert contradicted.interpretation is None
    assert LayoutProfile.from_json(good.to_json()) == good


def test_profile_cannot_suppress_coverage(make_document):
    doc = make_document(HEADER + '01/03/2027 OPERAÇÃO R$ 10,00', 'SEÇÃO FINANCEIRA R$ 20,00')
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.ABSENT,
                            balance_column=None, footer_rows=1)
    assert infer_layout(doc, profile=profile).status != AnalysisStatus.SUCCESS


@pytest.mark.parametrize('identity', ['unknown', 'institution-a', 'institution-b'])
def test_bank_and_account_identity_never_change_hypotheses(make_document, identity):
    doc = make_document(HEADER + '01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    baseline = infer_layout(doc)
    result = infer_layout(doc, context=StatementContext(bank_id=identity, account=replace(ACCOUNT, bank_id=identity)))
    assert result == baseline


def test_competing_scopes_abstain_before_any_financial_mix(make_document):
    doc = make_document(HEADER + '01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    result = infer_layout(doc)
    scope = result.hypotheses[0].scope
    other = replace(scope, region=replace(scope.region, region_id='another'))
    competing = infer_layout(doc, scope_candidates=(scope, other))
    assert competing.status == AnalysisStatus.AMBIGUOUS
    assert competing.blocking_capabilities == ('scope_selection',)
    assert competing.candidate_hypotheses == 2
    assert infer_layout(doc, scope_candidates=(other,)).status == AnalysisStatus.UNSUPPORTED


@pytest.mark.parametrize('mutation,constraint', [
    ('same_money', 'exclusive_monetary_ownership'), ('magnitude','direction_origin'),
    ('direction','direction_origin'), ('date_origin','scope_and_segment_origin'),
    ('missing_amount','monetary_coverage'), ('scope','scope_and_segment_origin')])
def test_hard_ownership_and_provenance_constraints(make_document, mutation, constraint):
    doc = make_document(HEADER + '01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    result = infer_layout(doc)
    h = result.hypotheses[0]
    prepared = prepare_composition(doc, result.profile, legacy_context=False)
    # Rows are immutable but reconstructed anew; hypotheses reference that exact input.
    h = replace(h, scope=prepared.scope, transaction_segments=prepared.segments,
                date_assignments=tuple(replace(d, segment=prepared.segments[i], source_row=prepared.segments[i].rows[0])
                                       for i,d in enumerate(h.date_assignments)),
                directions=tuple(replace(d, source_row=prepared.segments[i].rows[0]) for i,d in enumerate(h.directions)))
    role = h.monetary_roles[0]
    if mutation == 'same_money': h = replace(h, monetary_roles=(replace(role,running_balance=role.movement),))
    elif mutation == 'magnitude': h = replace(h, monetary_roles=(replace(role,magnitude=Decimal('11')),))
    elif mutation == 'direction': h = replace(h, directions=(replace(h.directions[0],sign=-1),))
    elif mutation == 'date_origin': h = replace(h,date_assignments=(replace(h.date_assignments[0],candidate=replace(h.date_assignments[0].candidate,value=date(2027,3,2))),))
    elif mutation == 'missing_amount': h = replace(h,monetary_roles=(replace(role,running_balance=None),))
    elif mutation == 'scope': h = replace(h,scope=replace(h.scope,region=replace(h.scope.region,region_id='foreign')))
    with pytest.raises(ConstraintViolation, match=constraint): constrain_hypothesis(prepared,h)


def test_checkpoint_without_pdf_source_cannot_finish_coverage(checkpoint_pdf):
    _, path = checkpoint_pdf
    result = infer_layout(extract_pdf(path)).interpretation
    if not result.statement.checkpoints:
        return
    statement = replace(result.statement, checkpoints=(replace(result.statement.checkpoints[0],source=None),))
    ledger = CoverageLedger(())
    with pytest.raises(FinancialCoverageError, match='exclusive financial source'):
        ledger.finish(statement, result.evidence)


def test_equivalent_description_span_syntax_is_not_ambiguous(make_document):
    result = infer_layout(make_document(HEADER + '01/03/2027 OPERAÇÃO ALFA R$ 10,00')).interpretation
    field = result.provenance.transactions[0]
    span = field.description_sources[0]
    split = (replace(span,word_end=span.word_start+1), replace(span,word_start=span.word_start+1))
    other = replace(result, provenance=replace(result.provenance,transactions=(replace(field,description_sources=split),)))
    assert material_key(result) == material_key(other)
    assert resolve_hypotheses((result,other)) == resolve_hypotheses((other,result))
    different = replace(other, provenance=replace(other.provenance, transactions=(replace(field,date_source=replace(field.date_source,row=999)),)))
    with pytest.raises(AmbiguousStatementError): resolve_hypotheses((result,different))


@pytest.mark.parametrize('recipe', ['descending_checkpoints','daily_checkpoints','sparse_checkpoints'])
def test_candidate_enumeration_order_is_metamorphically_irrelevant(recipe, write_layout_pdf, tmp_path):
    path = tmp_path / 'fixture.pdf'
    write_layout_pdf(LAYOUTS / recipe / 'pages.json',path)
    doc = extract_pdf(path)
    a,b = infer_layout(doc),infer_layout(doc,reverse_candidates=True)
    assert a.status == b.status == AnalysisStatus.SUCCESS
    assert a.interpretation == b.interpretation
    assert a.profile == b.profile
    assert sorted(h.semantic_key() for h in a.hypotheses) == sorted(h.semantic_key() for h in b.hypotheses)


@pytest.mark.parametrize('text,status', [
    (HEADER+'OPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00',AnalysisStatus.AMBIGUOUS),
    (HEADER+'01/03/2027 OPERAÇÃO R$ 11,00',AnalysisStatus.INVALID),
    ('01/03/2027 OPERAÇÃO R$ 10,00',AnalysisStatus.UNSUPPORTED)])
def test_candidate_order_never_changes_failure_status(make_document,text,status):
    a,b = infer_layout(make_document(text)),infer_layout(make_document(text),reverse_candidates=True)
    assert a.status == b.status == status
    assert a.blocking_capabilities == b.blocking_capabilities


@pytest.mark.parametrize('fixture', ['inter_pdf','synthetic_pdf','grouped_pdf'])
def test_legacy_operators_hypotheses_and_application_equivalence(request,fixture):
    path=request.getfixturevalue(fixture);document=extract_pdf(path)
    old,operators,current=(infer_layout(document,**mode) for mode in ({'legacy':True},{'operators':True},{}))
    if fixture == 'synthetic_pdf':
        # M0's closing declaration is inside its transaction area. The historical
        # generic grammar did not handle it; its specific parser is the oracle.
        old = operators
    assert old.status == operators.status == current.status == AnalysisStatus.SUCCESS
    assert old.interpretation.statement == operators.interpretation.statement == current.interpretation.statement
    assert old.interpretation.evidence == operators.interpretation.evidence == current.interpretation.evidence
    specific=InterParser() if fixture=='inter_pdf' else SyntheticParser() if fixture=='synthetic_pdf' else None
    analysis=analyze_pdf(path)
    assert analysis.status == AnalysisStatus.SUCCESS and analysis.candidate_hypotheses > 0
    if specific:
        reference=specific.parse(document)
        assert analysis.statement == reference
        assert export_analysis(analysis) == generate_ofx(reference)
    else:
        assert analysis.statement == current.interpretation.statement


def test_unknown_chronology_is_representable_but_not_checkpoint_verification(make_document):
    doc=make_document(HEADER+'01/03/2027 OPERAÇÃO R$ 10,00 R$ 110,00')
    r=infer_layout(doc)
    statement=replace(r.interpretation.statement,chronology=Chronology.UNKNOWN)
    assert validate_statement(statement).running_balance_verified == EvidenceStatus.NOT_AVAILABLE
    h=replace(r.hypotheses[0],chronology=Chronology.UNKNOWN)
    assert h.chronology == Chronology.UNDECLARED
    prepared=prepare_composition(doc,r.profile,legacy_context=False)
    from pdf_to_ofx.generic.operators.candidates import OperatorFailure
    # Rebind the original candidate references to this reconstructed input.
    h=replace(h,scope=prepared.scope,transaction_segments=prepared.segments,
        date_assignments=(replace(h.date_assignments[0],segment=prepared.segments[0],source_row=prepared.segments[0].rows[0]),),
        directions=(replace(h.directions[0],source_row=prepared.segments[0].rows[0]),))
    with pytest.raises(OperatorFailure,match='economic order'): constrain_hypothesis(prepared,h)


def test_both_m4_layout_recipes_preserve_three_way_equivalence(layout_pdf):
    document=extract_pdf(layout_pdf)
    results=[infer_layout(document,**mode) for mode in ({'legacy':True},{'operators':True},{})]
    assert all(r.status == AnalysisStatus.SUCCESS for r in results)
    assert all(r.interpretation.statement == results[0].interpretation.statement for r in results)
    assert all(r.interpretation.evidence == results[0].interpretation.evidence for r in results)


def test_group_and_row_date_alternatives_can_coexist(make_document):
    doc=make_document(HEADER+'01/03/2027\nOPERAÇÃO 02/03/2027 R$ 10,00')
    profile=LayoutProfile(DateMode.GROUPED,AmountMode.SIGNED,BalanceMode.ABSENT,balance_column=None)
    prepared=prepare_composition(doc,profile,legacy_context=False)
    domain=date_assignment_candidates(prepared.segments,prepared.candidates)[0]
    assert len(domain)==2
    result=infer_layout(doc)
    assert result.status==AnalysisStatus.AMBIGUOUS
    assert len(result.hypotheses)==2
    # An explicit structural constraint narrows date origin, never the math.
    assert infer_layout(doc,profile=profile).status==AnalysisStatus.SUCCESS


@pytest.mark.parametrize('change,expected', [('10,00',AnalysisStatus.SUCCESS),('25,00',AnalysisStatus.INVALID)])
def test_declared_credit_debit_totals_are_independent_incremental_controls(make_document,change,expected):
    text=('Período: 01/03/2027 a 03/03/2027\nTotal de entradas: +R$ 20,00\n'
          'Total de saídas: -R$ 10,00\n01/03/2027 ALFA +R$ '+change+'\n'
          '02/03/2027 BETA +R$ 10,00\n03/03/2027 GAMA -R$ 10,00')
    result=infer_layout(make_document(text))
    assert result.status==expected
    if expected==AnalysisStatus.SUCCESS:
        evidence=result.interpretation.evidence
        assert evidence.has_financial_support
        assert evidence.credit_total_verified==evidence.debit_total_verified==EvidenceStatus.VERIFIED
        assert evidence.opening_closing_reconciled==EvidenceStatus.NOT_AVAILABLE
    else:
        assert dict(result.pruned_constraints)['declared_credits']==1
        assert result.candidate_hypotheses==0


def test_synthetic_reverse_profile_does_not_require_legacy_ascending_validation(write_layout_pdf,tmp_path):
    path=tmp_path/'descending.pdf'
    write_layout_pdf(LAYOUTS/'descending_checkpoints/pages.json',path)
    doc=extract_pdf(path)
    inferred=infer_layout(doc)
    profile=LayoutProfile.from_json(inferred.profile.to_json())
    assert infer_layout(doc,profile=profile).interpretation==inferred.interpretation


def test_sparse_checkpoint_corruption_blocks_export(checkpoint_pdf):
    name,path=checkpoint_pdf
    if name.startswith('descending'):
        return
    analysis=analyze_pdf(path)
    point=analysis.statement.checkpoints[0]
    corrupted=replace(analysis,statement=replace(analysis.statement,
        checkpoints=(replace(point,balance=point.balance+Decimal('1')), *analysis.statement.checkpoints[1:])))
    with pytest.raises(StatementValidationError): export_analysis(corrupted,ACCOUNT)


def test_empty_scope_domain_is_unsupported(make_document):
    result=infer_layout(make_document(HEADER+'01/03/2027 OPERAÇÃO R$ 10,00'),scope_candidates=())
    assert result.status==AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities==('scope_boundary_unknown',)


def test_isolated_daily_balance_is_not_a_verified_financial_link(make_document):
    text=('Período: 01/03/2027 a 03/03/2027\nTotal de entradas: +R$ 10,00\n'
          'Total de saídas: -R$ 3,00\n01/03/2027 Saldo do dia: R$ 107,00\n'
          'ALFA +R$ 10,00\nBETA -R$ 3,00\n02/03/2027\nGAMA R$ 0,00')
    result=infer_layout(make_document(text))
    assert result.status==AnalysisStatus.SUCCESS
    assert result.interpretation.evidence.checkpoints_verified==EvidenceStatus.NOT_AVAILABLE
    assert result.interpretation.evidence.daily_balances_verified==EvidenceStatus.NOT_AVAILABLE


def test_both_strong_daily_checkpoints_can_work_without_outer_balances(write_layout_pdf,tmp_path):
    import json
    recipe=json.loads((LAYOUTS/'daily_checkpoints/pages.json').read_text())
    recipe['pages'][0]=[row for row in recipe['pages'][0] if row['y'] not in (50,70)]
    source=tmp_path/'daily.json';source.write_text(json.dumps(recipe))
    path=tmp_path/'daily.pdf';write_layout_pdf(source,path)
    result=infer_layout(extract_pdf(path))
    assert result.status==AnalysisStatus.SUCCESS
    assert result.interpretation.evidence.has_financial_support
    assert result.interpretation.evidence.checkpoint_links==1
    assert result.interpretation.evidence.opening_closing_reconciled==EvidenceStatus.NOT_AVAILABLE


@pytest.mark.parametrize('amount',[Decimal('NaN'),Decimal('Infinity'),1.0])
def test_checkpoint_math_never_accepts_non_decimal_or_nonfinite_money(amount):
    with pytest.raises(ConstraintViolation,match='checkpoint_domain'):
        check_checkpoints((amount,),())


def test_changing_candidate_order_cannot_turn_budget_exhaustion_into_success(make_document):
    doc=make_document(HEADER+'OPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00')
    a=infer_layout(doc,budget=SearchBudget(max_hypotheses=2))
    b=infer_layout(doc,budget=SearchBudget(max_hypotheses=2),reverse_candidates=True)
    assert a.status==b.status==AnalysisStatus.AMBIGUOUS
    assert a.budget_exhausted and b.budget_exhausted
    assert a.interpretation is b.interpretation is None
    assert a.blocking_capabilities==b.blocking_capabilities==('search_budget',)


def test_unsigned_group_with_extra_monetary_region_abstains_without_crashing(write_layout_pdf,tmp_path):
    import json
    recipe=json.loads((LAYOUTS/'grouped_subtotals_wrapped/pages.json').read_text())
    recipe['pages'][0][10]['cells'][-1][1]='10,00 1,00'
    source=tmp_path/'extra.json';source.write_text(json.dumps(recipe))
    path=tmp_path/'extra.pdf';write_layout_pdf(source,path)
    result=infer_layout(extract_pdf(path))
    assert result.status==AnalysisStatus.UNSUPPORTED
    assert result.blocking_capabilities==('amount_role_inference',)
