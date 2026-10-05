"""Compose local structural operators; retain legacy summary readers for M7.

Each stage consumes candidates from the previous stage. This module never calls
a complete legacy grammar, consults institution identity or ranks financial data.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.errors import AmbiguousStatementError, FinancialCoverageError, StatementParseError
from pdf_to_ofx.domain.evidence import ChronologySource, DocumentRegion, FinancialRole, Interpretation, MonetaryAssignment, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology, Statement, Transaction
from pdf_to_ofx.generic.operators.amounts import AmountRoles, MonetaryDomain, MonetaryObservation, RoleDecision, infer_amount_roles, infer_control_roles
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.chronology import infer_chronology
from pdf_to_ofx.generic.operators.dates import DatedSegment, attribute_dates
from pdf_to_ofx.generic.operators.directions import DirectionEvidence, infer_direction
from pdf_to_ofx.generic.operators.financial import financial_evidence
from pdf_to_ofx.generic.operators.pages import continue_pages
from pdf_to_ofx.generic.operators.relations import generate_relational_domains, observe_money
from pdf_to_ofx.generic.operators.ownership import control_role_evidence
from pdf_to_ofx.generic.operators.scopes import FinancialScope, segment_scopes
from pdf_to_ofx.generic.operators.transactions import RowCandidate, TransactionSegment, description_intervals, semantic_candidates, segment_transactions
from pdf_to_ofx.generic.parser import StatementContext, read_context
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage, canonical_sources
from pdf_to_ofx.generic.structure import Row, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


if TYPE_CHECKING:
    from pdf_to_ofx.generic.hypotheses import StructuralHypothesis


def source_key(sources: tuple[SourceSpan, ...]) -> tuple:
    """Splitting a span changes syntax, not the underlying PDF tokens."""
    return tuple(sorted({(s.region_id, s.basis, s.page, s.row, word)
                         for s in sources for word in range(s.word_start, s.word_end)}))


def material_key(hypothesis: Interpretation) -> tuple:
    s, p = hypothesis.statement, hypothesis.provenance
    fields = tuple((source_key((f.date_source,)) if f.date_source else (),
        source_key(f.description_sources), source_key((f.amount_source,)) if f.amount_source else (),
        source_key((f.balance_source,)) if f.balance_source else (),
        source_key((f.direction_source,)) if f.direction_source else ()) for f in p.transactions)
    return (s.period_start, s.period_end, s.opening_balance, s.closing_balance,
            s.transactions, hypothesis.evidence, fields,
            p.chronology.economic_order if p.chronology else None,
            tuple(sorted((source_key((a.source,)), a.role, a.transaction_index)
                         for a in p.monetary_regions)),
            tuple((c.after, c.balance, c.kind, source_key((c.source,)) if c.source else ()) for c in s.checkpoints))


def resolve_hypotheses(hypotheses: tuple[Interpretation, ...]) -> Interpretation:
    """Provenance syntax is immaterial; distinct financial outcomes abstain."""
    unique = {}
    for hypothesis in hypotheses:
        key = material_key(hypothesis)
        # Canonical representation among materially equivalent candidates only.
        if key not in unique or repr(hypothesis) < repr(unique[key]):
            unique[key] = hypothesis
    if len(unique) > 1:
        raise AmbiguousStatementError("Material financial interpretations cannot be distinguished uniquely.")
    if not unique:
        raise OperatorFailure("financial_evidence", "No financial hypothesis survived the constraints.")
    return next(iter(unique.values()))


@dataclass(frozen=True, slots=True)
class CompositionInput:
    original: tuple[Row, ...]
    scope: FinancialScope
    frames: tuple[DocumentRegion, ...]
    candidates: tuple[RowCandidate, ...]
    segments: tuple[TransactionSegment, ...]
    context: StatementContext
    totals: tuple[tuple[str, Decimal], ...]
    declarations: tuple[MonetaryAssignment, ...]
    profile: LayoutProfile
    monetary_domains: tuple[MonetaryDomain, ...] = ()
    monetary_observations: tuple[MonetaryObservation, ...] = ()
    role_evidence: tuple[RoleDecision, ...] = ()


def bind_declarations(prepared: CompositionInput, assignments: tuple[MonetaryAssignment, ...]) -> CompositionInput:
    from pdf_to_ofx.generic.operators.context import bind_context_roles
    context = bind_context_roles(prepared.context, prepared.monetary_domains, assignments)
    return replace(prepared, context=context, declarations=(*prepared.declarations, *assignments))


def prepare_composition(document: ExtractedDocument, profile: LayoutProfile,
                        *, context: StatementContext | None = None,
                        legacy_context: bool = True) -> CompositionInput:
    original = reconstruct_rows(document, profile.tolerances)
    coverage = VisualCoverage(original, profile.tolerances)
    try:
        rows = continue_pages(original, profile)
    except OperatorFailure:
        raise
    except StatementParseError as error:
        raise OperatorFailure("page_continuation", str(error)) from error
    scopes, frames = segment_scopes(original, rows, coverage)
    if len(scopes) != 1:
        raise OperatorFailure("scope_segmentation", "Automatic conversion requires one coherent financial scope.")
    coverage.regions = tuple(s.region for s in scopes) + frames
    try:
        candidates = semantic_candidates(scopes[0].rows, profile)
    except OperatorFailure:
        raise
    except StatementParseError as error:
        raise OperatorFailure("semantic_candidates", str(error)) from error
    start = next((c.position for c in candidates if c.dates and (
        c.kind in {"date_heading", "flow"} or c.kind == "content" and c.amounts)), None)
    if start is None:
        raise OperatorFailure("date_attribution", "No supported full-year dated transaction area was found.")
    # Declarations remain outside transaction segmentation, in the same scope.
    declaration_kinds = {"balance_declaration"} if legacy_context else {"balance_declaration", "monetary_only"}
    body = tuple(c for c in candidates[start:] if c.kind not in declaration_kinds)
    declarations = tuple(c.row for c in candidates[:start]) + tuple(
        c.row for c in candidates[start:] if c.kind in declaration_kinds)
    observations = observe_money(candidates, coverage)
    domains = ()
    role_evidence = []
    try:
        if not legacy_context:
            from pdf_to_ofx.generic.operators.context import read_financial_context
            context, totals, domains = read_financial_context(declarations, profile, context, coverage, role_evidence)
        elif profile.amount_mode == AmountMode.GROUP_SUBTOTAL:
            from pdf_to_ofx.generic.grouped import _context
            context, totals = _context(declarations, profile, context, coverage)
        else:
            context = read_context(declarations, profile, context, coverage)
            totals = {}
    except OperatorFailure:
        raise
    except StatementParseError as error:
        raise OperatorFailure("financial_context", str(error)) from error
    segments = segment_transactions(body, profile)
    sources = [domain.source for domain in domains]
    if (len(sources) != len(set(sources)) or any(s not in coverage.expected or s in coverage.assignments for s in sources)):
        raise FinancialCoverageError("Monetary domains repeat or contradict source ownership.")
    if not legacy_context:
        domains = generate_relational_domains(domains, observations, candidates, segments, coverage, profile)
    evidence = tuple(role_evidence) if not legacy_context else tuple(
        RoleDecision(a.source, a.role, "declaration_label", True, "explicit_declared_role", (a.source,))
        for a in coverage.assignments.values())
    if not legacy_context:
        evidence += control_role_evidence(body, segments, coverage, profile)
    return CompositionInput(original, scopes[0], frames, body, segments, context,
                            tuple(totals.items()), tuple(coverage.assignments.values()), profile, domains, observations, evidence)


def materialize_composition(prepared: CompositionInput, dated: tuple[DatedSegment, ...],
                            amounts: tuple[AmountRoles, ...], directions: tuple[DirectionEvidence, ...],
                            chronology: Chronology, *, checkpoints: tuple[BalanceCheckpoint, ...] = (),
                            allow_checkpoints: bool = False) -> Interpretation:
    profile, body, context = prepared.profile, prepared.candidates, prepared.context
    if allow_checkpoints:
        checkpoints = tuple(p for p in checkpoints if p.kind == "sparse_checkpoint" or
            p.kind == "daily_balance" and any(r.running_balance is None for r in amounts))
    coverage = VisualCoverage(prepared.original, profile.tolerances)
    coverage.regions = (prepared.scope.region, *prepared.frames)
    for assignment in prepared.declarations:
        coverage.claim(assignment.source, assignment.role, assignment.transaction_index)
    transactions = []
    date_sources = []
    for index, (item, roles, direction) in enumerate(zip(dated, amounts, directions, strict=True)):
        segment = item.segment
        own_date = item.candidate if item.source_row.position == segment.position else None
        description_regions = description_intervals(segment, own_date,
            reject_balance_heading=profile.amount_mode != AmountMode.GROUP_SUBTOTAL)
        description = " ".join(w.text for row, begin, end in description_regions for w in row.words[begin:end])
        head = segment.rows[0].row
        movement_source = coverage.span(head, roles.movement.start, roles.movement.end)
        balance_source = (coverage.span(head, roles.running_balance.start, roles.running_balance.end)
                          if roles.running_balance else None)
        date_source = coverage.span(item.source_row.row, item.candidate.start, item.candidate.end)
        date_sources.append(date_source)
        direction_source = coverage.span(direction.source_row.row, direction.word_start, direction.word_end)
        coverage.monetary(head, roles.movement, FinancialRole.MOVEMENT, index)
        if roles.running_balance:
            coverage.monetary(head, roles.running_balance, FinancialRole.RUNNING_BALANCE, index)
        coverage.transaction(date_source, direction_source, *(coverage.span(c.row) for c in segment.rows))
        coverage.field_sources(index, date=date_source,
            description=canonical_sources(tuple(coverage.span(row, begin, end) for row, begin, end in description_regions)),
            amount=movement_source, balance=balance_source, direction=direction_source,
            direction_basis=direction.basis, economic_order=(None if chronology == Chronology.UNKNOWN else
                len(dated) - index - 1 if chronology == Chronology.DESCENDING else index))
        transactions.append(Transaction(item.candidate.value, description, direction.apply(roles.magnitude),
                            roles.running_balance.money.amount if roles.running_balance else None))
    for candidate in body:
        for control in infer_control_roles(candidate, profile.balance_mode, allow_checkpoints=allow_checkpoints):
            coverage.monetary(candidate.row, control.region, control.role)
    order = tuple(range(len(transactions)))
    economic = order[::-1] if chronology == Chronology.DESCENDING else order
    coverage.chronology = ChronologySource(order, economic if chronology != Chronology.UNKNOWN else None,
                                          "constrained_order" if allow_checkpoints else "nondecreasing_full_dates", tuple(date_sources))
    statement = Statement(context.bank_id, "generic-structural-v1", context.period_start,
        context.period_end, context.opening_balance, context.closing_balance, tuple(transactions), context.account,
        chronology=chronology, running_balances_required=profile.balance_mode == BalanceMode.RUNNING and all(r.running_balance for r in amounts),
        checkpoints=checkpoints)
    evidence = financial_evidence(statement, body, tuple(item.group_position for item in dated),
        tuple(direction.group_position for direction in directions), dict(prepared.totals), profile.balance_mode,
        control_sources={c.position: coverage.span(c.row, c.amounts[0].start, c.amounts[0].end)
                         for c in body if c.kind == "date_heading" and c.amounts})
    return coverage.finish(statement, evidence)


def interpret_composed(document: ExtractedDocument, profile: LayoutProfile,
                       *, context: StatementContext | None = None) -> Interpretation:
    """M7 fixed composition remains a regression oracle for the M8 search."""
    prepared = prepare_composition(document, profile, context=context)
    dated = attribute_dates(prepared.segments, prepared.candidates, profile.date_mode,
                            carry_across_pages=profile.carry_date_across_pages)
    amounts = tuple(infer_amount_roles(item.segment, profile) for item in dated)
    directions = tuple(infer_direction(item.segment, roles, profile.amount_mode, prepared.candidates,
        profile.tolerances, carry_across_pages=profile.carry_date_across_pages)
        for item, roles in zip(dated, amounts, strict=True))
    for candidate in prepared.candidates:
        if (candidate.kind == "date_heading" and profile.date_mode == DateMode.GROUPED
                and profile.amount_mode != AmountMode.GROUP_SUBTOTAL
                and not any(item.group_position == candidate.position for item in dated)):
            raise OperatorFailure("transaction_segmentation", "A date group contains no transactions.")
    transactions = tuple(Transaction(item.candidate.value, "candidate", direction.apply(roles.magnitude))
                         for item, roles, direction in zip(dated, amounts, directions, strict=True))
    chronology, _ = infer_chronology(transactions, ())
    if chronology == Chronology.UNDECLARED:
        raise OperatorFailure("chronology_inference", "Economic order is unresolved; reverse ordering is not supported yet.")
    return materialize_composition(prepared, dated, amounts, directions, chronology)


def structural_key(hypothesis: StructuralHypothesis) -> tuple:
    """Material fields and original token ownership, without segment syntax."""
    references = {id(row): span for row, span in zip(hypothesis.scope.rows, hypothesis.scope.region.spans, strict=True)}

    def tokens(row, begin, end):
        return replace(references[id(row)], word_start=begin, word_end=end)

    output = []
    for dated, roles, direction in zip(hypothesis.date_assignments, hypothesis.monetary_roles, hypothesis.directions, strict=True):
        own = dated.candidate if dated.source_row.position == dated.segment.position else None
        regions = description_intervals(dated.segment, own, reject_balance_heading=direction.basis != "signed_group_subtotal")
        description = " ".join(w.text for row, begin, end in regions for w in row.words[begin:end])
        output.append((dated.candidate.value, description, direction.apply(roles.magnitude),
                       roles.running_balance.money.amount if roles.running_balance else None,
                       source_key((tokens(dated.source_row.row, dated.candidate.start, dated.candidate.end),)),
                       source_key(tuple(tokens(row, begin, end) for row, begin, end in regions)),
                       source_key((tokens(dated.segment.rows[0].row, roles.movement.start, roles.movement.end),)),
                       source_key((tokens(direction.source_row.row, direction.word_start, direction.word_end),))))
    order = tuple(range(len(output)))
    economic = None if hypothesis.chronology == Chronology.UNKNOWN else order[::-1] if hypothesis.chronology == Chronology.DESCENDING else order
    return hypothesis.scope.region.region_id, tuple(output), economic, hypothesis.monetary_assignments
