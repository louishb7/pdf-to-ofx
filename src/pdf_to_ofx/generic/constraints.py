"""Hard constraints over partial operator assignments, before materialization."""

from __future__ import annotations

from datetime import date
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology
from pdf_to_ofx.domain.evidence import FinancialRole, MonetaryAssignment, SourceSpan
from pdf_to_ofx.generic.composition import CompositionInput, bind_declarations, source_key
from pdf_to_ofx.generic.operators.amounts import infer_control_roles
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.dates import DatedSegment, date_assignment_candidates
from pdf_to_ofx.generic.operators.financial import checkpoint_candidates
from pdf_to_ofx.generic.operators.directions import DirectionEvidence, infer_direction
from pdf_to_ofx.generic.operators.amounts import AmountRoles
from pdf_to_ofx.generic.operators.transactions import description_intervals
from pdf_to_ofx.generic.operators.ownership import control_intervals, flow_members, movement_sources
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.semantics import MoneyRegion
from pdf_to_ofx.validation.checkpoints import ConstraintViolation, _sum, check_checkpoints
from pdf_to_ofx.validation.controls import ControlInterval, overlaps, validate_control_intervals
from pdf_to_ofx.validation.coverage import source_has_role

if TYPE_CHECKING:
    from pdf_to_ofx.generic.hypotheses import StructuralHypothesis


@dataclass(frozen=True, slots=True)
class ConstraintFacts:
    coverage: VisualCoverage
    date_domains: tuple[tuple[DatedSegment, ...], ...]
    controls: tuple[MonetaryAssignment, ...]
    transaction_sources: tuple[SourceSpan, ...]
    directions: tuple[tuple[tuple[MoneyRegion, DirectionEvidence], ...], ...]


def observe_constraint_facts(prepared: CompositionInput) -> ConstraintFacts:
    """Inventory once; partial checks never mutate the shared coverage ledger."""
    coverage = VisualCoverage(prepared.original, prepared.profile.tolerances)
    controls = tuple(MonetaryAssignment(coverage.span(c.row, control.region.start, control.region.end), control.role)
        for c in prepared.candidates for control in infer_control_roles(c, prepared.profile.balance_mode, allow_checkpoints=True))
    sources = tuple(coverage.span(s.rows[0].row, region.start, region.end) for s in prepared.segments for region in s.rows[0].amounts)
    directions = []
    for segment in prepared.segments:
        candidates = []
        for region in segment.rows[0].amounts:
            try:
                candidates.append((region, infer_direction(segment, AmountRoles(region, region.money.amount.copy_abs(), None),
                    prepared.profile.amount_mode, prepared.candidates, prepared.profile.tolerances,
                    carry_across_pages=prepared.profile.carry_date_across_pages)))
            except OperatorFailure:
                continue
        directions.append(tuple(candidates))
    return ConstraintFacts(coverage, date_assignment_candidates(prepared.segments, prepared.candidates,
        carry_across_pages=prepared.profile.carry_date_across_pages), controls, sources, tuple(directions))


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    checkpoints: tuple[BalanceCheckpoint, ...]
    controls: tuple[ControlInterval, ...]


def evaluate_hypothesis(prepared: CompositionInput, hypothesis: StructuralHypothesis,
                        *, fixed_dates: tuple[date, ...] | None = None,
                        facts: ConstraintFacts | None = None) -> ConstraintResult:
    """Hard constraints act on partial assignments, before a Statement exists."""
    if hypothesis.monetary_domains != prepared.monetary_domains:
        raise ConstraintViolation("monetary_domain_membership")
    facts = facts or observe_constraint_facts(prepared)
    prepared = bind_declarations(prepared, hypothesis.monetary_assignments)
    coverage = facts.coverage
    ownership = {a.source: a for a in (*prepared.declarations, *facts.controls)}
    owned = {a.source for a in prepared.declarations}
    if len(owned) != len(prepared.declarations):
        raise ConstraintViolation("exclusive_monetary_ownership")
    rows = {id(row) for row in hypothesis.scope.rows}
    count = len(prepared.segments)
    if (hypothesis.scope != prepared.scope or hypothesis.transaction_segments != prepared.segments
            or not isinstance(hypothesis.chronology, Chronology)
            or not len(hypothesis.date_assignments) == len(hypothesis.monetary_roles) == len(hypothesis.directions)
            or len(hypothesis.date_assignments) > count):
        raise ConstraintViolation("scope_and_segment_origin")
    for domain, assignment in zip(prepared.monetary_domains, hypothesis.monetary_assignments):
        if domain.boundary is None:
            continue
        cut = count - domain.boundary if hypothesis.chronology == Chronology.DESCENDING else domain.boundary
        expected = FinancialRole.OPENING_BALANCE if cut == 0 else FinancialRole.CLOSING_BALANCE if cut == count else FinancialRole.SPARSE_CHECKPOINT
        if assignment.role != expected:
            raise ConstraintViolation("control_economic_frontier")
        for segment, roles in zip(prepared.segments, hypothesis.monetary_roles):
            if roles.running_balance is not None and roles.running_balance not in segment.rows[0].amounts:
                raise ConstraintViolation("monetary_origin")
            if roles.running_balance is None or segment.rows[0].amounts.index(roles.running_balance) != domain.column:
                raise ConstraintViolation("control_column_ownership")
    reserved = tuple(a.source for a in (*prepared.declarations, *facts.controls)) + tuple(d.source for d in prepared.monetary_domains)
    if any(overlaps(source, s) for source in reserved for s in facts.transaction_sources):
        raise ConstraintViolation("exclusive_monetary_ownership")
    for control in facts.controls:
        source = control.source
        if source not in coverage.expected or source in owned:
            raise ConstraintViolation("exclusive_monetary_ownership")
        owned.add(source)
    values: list[Decimal | None] = [None] * count
    columns = set()
    descriptions = set()
    date_domains = facts.date_domains
    for index, (dated, roles, direction) in enumerate(zip(hypothesis.date_assignments, hypothesis.monetary_roles, hypothesis.directions, strict=True)):
        segment = hypothesis.transaction_segments[index]
        if (segment != dated.segment or any(id(c.row) not in rows for c in segment.rows)
                or id(dated.source_row.row) not in rows or id(direction.source_row.row) not in rows
                or dated.candidate not in dated.source_row.dates or dated not in date_domains[index]):
            raise ConstraintViolation("scope_and_segment_origin")
        if not prepared.context.period_start <= dated.candidate.value <= prepared.context.period_end:
            raise ConstraintViolation("date_period")
        candidates = segment.rows[0].amounts
        if roles.movement not in candidates or roles.running_balance is not None and roles.running_balance not in candidates:
            raise ConstraintViolation("monetary_origin")
        expected_direction = next((d for r, d in facts.directions[index] if r == roles.movement), None)
        if direction != expected_direction or roles.magnitude != roles.movement.money.amount.copy_abs():
            raise ConstraintViolation("direction_origin")
        if len(candidates) == 2:
            columns.add(candidates.index(roles.movement))
        for region in (roles.movement, roles.running_balance):
            if region is None:
                continue
            source = coverage.span(segment.rows[0].row, region.start, region.end)
            if source not in coverage.expected or source in owned:
                raise ConstraintViolation("exclusive_monetary_ownership")
            owned.add(source)
            role = FinancialRole.MOVEMENT if region is roles.movement else FinancialRole.RUNNING_BALANCE
            ownership[source] = MonetaryAssignment(source, role, index)
        own_date = dated.candidate if dated.source_row.position == segment.position else None
        spans = description_intervals(segment, own_date, reject_balance_heading=direction.basis != "signed_group_subtotal")
        tokens = set(source_key(tuple(coverage.span(row, begin, end) for row, begin, end in spans)))
        if descriptions & tokens:
            raise ConstraintViolation("exclusive_description_origin")
        descriptions.update(tokens)
        economic = index if hypothesis.chronology != Chronology.DESCENDING else count - index - 1
        values[economic] = direction.apply(roles.magnitude)
    if len(columns) > 1:
        raise ConstraintViolation("column_role_continuity")
    if hypothesis.complete and owned != coverage.expected:
        raise ConstraintViolation("monetary_coverage")
    if hypothesis.complete:
        dates = tuple(d.candidate.value for d in hypothesis.date_assignments)
    else:
        dates = fixed_dates
    if hypothesis.chronology != Chronology.UNKNOWN:
        available_dates = dates if dates is not None else tuple(d.candidate.value for d in hypothesis.date_assignments)
        economic_dates = available_dates[::-1] if hypothesis.chronology == Chronology.DESCENDING else available_dates
        if any(a > b for a, b in zip(economic_dates, economic_dates[1:])):
            raise ConstraintViolation("chronology_dates")
    points = checkpoint_candidates(prepared, hypothesis, coverage, dates)
    if hypothesis.chronology == Chronology.UNKNOWN:
        if any(p.kind in {"running_balance", "daily_balance", "sparse_checkpoint"} for p in points):
            raise OperatorFailure("chronology_unknown", "Checkpoint positions require an economic order hypothesis.")
    controls = control_intervals(prepared, hypothesis, coverage, points, tuple(values))
    validate_control_intervals(controls, prepared.scope.region.region_id,
        movement_order=movement_sources(prepared, hypothesis, coverage), scope_sources=prepared.scope.region.spans)
    if any(p.source is not None and not source_has_role(p.source, p.kind, ownership) for p in points):
        raise ConstraintViolation("control_origin")
    check_checkpoints(tuple(values), points)
    for candidate in prepared.candidates:
        if candidate.kind != "flow":
            continue
        members = flow_members(candidate, prepared.candidates, prepared.segments)
        selected = [hypothesis.directions[i].apply(hypothesis.monetary_roles[i].magnitude)
                    for i in members if i < len(hypothesis.directions)]
        expected = candidate.amounts[0].money.amount
        if any(value != 0 and value.is_signed() != expected.is_signed() for value in selected):
            raise ConstraintViolation("subtotal_direction")
        if _sum(selected).copy_abs() > expected.copy_abs():
            raise ConstraintViolation("subtotal_partial_bound")
        if len(selected) == len(members) and (not members or _sum(selected) != expected):
            raise ConstraintViolation("subtotal_reconciliation")
    selected = [value for value in values if value is not None]
    credits, debits = _sum([v for v in selected if v >= 0]), _sum([v for v in selected if v < 0])
    for name, total in prepared.totals:
        if name not in {"credits", "debits"}:
            continue
        actual = credits if name == "credits" else debits
        if (name == "credits" and actual > total or name == "debits" and actual < total
                or hypothesis.complete and actual != total):
            raise ConstraintViolation(f"declared_{name}")
    if hypothesis.complete:
        for candidate in prepared.candidates:
            if (candidate.kind == "date_heading" and not candidate.amounts
                    and not any(s.position > candidate.position and not any(
                        c.kind == "date_heading" and candidate.position < c.position < s.position
                        for c in prepared.candidates) for s in prepared.segments)):
                raise OperatorFailure("transaction_segmentation", "A date group contains no transactions.")
    return ConstraintResult(points, controls)


def constrain_hypothesis(prepared: CompositionInput, hypothesis: StructuralHypothesis,
                         *, fixed_dates: tuple[date, ...] | None = None,
                         facts: ConstraintFacts | None = None) -> tuple[BalanceCheckpoint, ...]:
    """Compatibility view; both callers use exactly the same invariants."""
    return evaluate_hypothesis(prepared, hypothesis, fixed_dates=fixed_dates, facts=facts).checkpoints
