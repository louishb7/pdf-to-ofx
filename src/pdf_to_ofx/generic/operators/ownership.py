"""Ordered control ownership, independent of reconciliation arithmetic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology
from pdf_to_ofx.generic.operators.amounts import RoleDecision, infer_control_roles
from pdf_to_ofx.validation.controls import ControlInterval

if TYPE_CHECKING:
    from decimal import Decimal
    from pdf_to_ofx.generic.composition import CompositionInput
    from pdf_to_ofx.generic.hypotheses import StructuralHypothesis
    from pdf_to_ofx.generic.operators.transactions import RowCandidate, TransactionSegment
    from pdf_to_ofx.generic.provenance import VisualCoverage
    from pdf_to_ofx.generic.profile import LayoutProfile


def flow_members(candidate: RowCandidate, candidates: tuple[RowCandidate, ...],
                 segments: tuple[TransactionSegment, ...]) -> tuple[int, ...]:
    end = next((c.position for c in candidates if c.position > candidate.position and
                c.kind in {"flow", "date_heading"}), None)
    return tuple(i for i, s in enumerate(segments) if candidate.position < s.position and
                 (end is None or s.position < end))


def movement_sources(prepared: CompositionInput, hypothesis: StructuralHypothesis,
                     coverage: VisualCoverage) -> tuple[SourceSpan | None, ...]:
    sources = [None] * len(prepared.segments)
    for index, (segment, roles) in enumerate(zip(prepared.segments, hypothesis.monetary_roles)):
        economic = len(sources) - index - 1 if hypothesis.chronology == Chronology.DESCENDING else index
        sources[economic] = coverage.span(segment.rows[0].row, roles.movement.start, roles.movement.end)
    return tuple(sources)


def control_role_evidence(candidates: tuple[RowCandidate, ...], segments: tuple[TransactionSegment, ...],
                          coverage: VisualCoverage, profile: LayoutProfile) -> tuple[RoleDecision, ...]:
    output = []
    for candidate in candidates:
        for control in infer_control_roles(candidate, profile.balance_mode, allow_checkpoints=True):
            if control.role == FinancialRole.SUBTOTAL:
                members = flow_members(candidate, candidates, segments)
                rule = 'declared_flow_group' if members else 'empty_declared_flow_group'
                admitted = bool(members)
            elif control.role == FinancialRole.DAILY_BALANCE:
                members = ()
                rule, admitted = 'declared_dated_boundary', True
            else:
                members = tuple(i for i, s in enumerate(segments) if s.position < candidate.position)
                rule = 'declared_transaction_cut'
                admitted = True
            source = coverage.span(candidate.row, control.region.start, control.region.end)
            output.append(RoleDecision(source, control.role, 'declared_control', admitted, rule,
                (coverage.span(candidate.row), *(coverage.span(segments[i].rows[0].row) for i in members))))
    return tuple(output)


def control_intervals(prepared: CompositionInput, hypothesis: StructuralHypothesis, coverage: VisualCoverage,
                      points: tuple[BalanceCheckpoint, ...], values: tuple[Decimal | None, ...]) -> tuple[ControlInterval, ...]:
    sources = movement_sources(prepared, hypothesis, coverage)
    output = []
    boundaries: dict[int, list[BalanceCheckpoint]] = {}
    for point in points:
        if point.source is not None:
            boundaries.setdefault(point.after, []).append(point)
    previous = None
    opening = None
    for after, group in sorted(boundaries.items()):
        group.sort(key=lambda p: (p.kind, p.source.page, p.source.row, p.source.word_start))
        for point in group:
            # Each printed declaration has one owner. A period closing balance
            # uses the opening anchor; intermediate balances use the prior cut.
            reference = opening if point.kind == 'closing_balance' and opening else previous
            begin = reference.after if reference else after
            output.append(ControlInterval(point.source, tuple(s for s in sources[begin:after] if s is not None),
                FinancialRole(point.kind), begin, after, (reference.source,) if reference else ()))
        if after == 0:
            opening = next((p for p in group if p.kind == 'opening_balance'), None)
        previous = group[0]
    for candidate in prepared.candidates:
        if candidate.kind != 'flow':
            continue
        members = flow_members(candidate, prepared.candidates, prepared.segments)
        economic = sorted(len(sources) - i - 1 if hypothesis.chronology == Chronology.DESCENDING else i for i in members)
        if not economic:
            # The understood empty group is rejected by subtotal reconciliation.
            continue
        region = candidate.amounts[0]
        output.append(ControlInterval(coverage.span(candidate.row, region.start, region.end),
            tuple(sources[i] for i in economic if sources[i] is not None), FinancialRole.SUBTOTAL,
            economic[0], economic[-1] + 1))
    for declaration in prepared.declarations:
        if declaration.role not in {FinancialRole.CREDIT_TOTAL, FinancialRole.DEBIT_TOTAL}:
            continue
        indexes = tuple(i for i, value in enumerate(values) if value is not None and
                        (value >= 0 if declaration.role == FinancialRole.CREDIT_TOTAL else value < 0))
        output.append(ControlInterval(declaration.source, tuple(sources[i] for i in indexes if sources[i] is not None),
            declaration.role, 0, len(sources), movement_indexes=indexes))
    return tuple(output)
