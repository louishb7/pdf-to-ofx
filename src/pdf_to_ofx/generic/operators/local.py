"""Compose structurally complete local candidates, before financial constraints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.evidence import FinancialRole, MonetaryAssignment, SourceSpan
from pdf_to_ofx.generic.operators.amounts import (
    AmountRoles, EmptyDomainCause, MonetaryDiagnostic, RoleDecision, amount_role_candidates,
)
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.dates import DatedSegment, date_assignment_candidates
from pdf_to_ofx.generic.operators.directions import DirectionEvidence, infer_direction
from pdf_to_ofx.generic.operators.transactions import description_intervals
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage, canonical_sources

if TYPE_CHECKING:
    from pdf_to_ofx.generic.composition import CompositionInput


@dataclass(frozen=True, slots=True)
class TransactionCandidate:
    dated: DatedSegment
    roles: AmountRoles
    direction: DirectionEvidence
    date_source: SourceSpan
    assignments: tuple[MonetaryAssignment, ...]


@dataclass(frozen=True, slots=True)
class LocalDomains:
    dates: tuple[tuple[DatedSegment, ...], ...]
    amounts: tuple[tuple[AmountRoles, ...], ...]
    alternatives: tuple[tuple[TransactionCandidate, ...], ...]
    refusals: tuple[str, ...] = ()
    decisions: tuple[RoleDecision, ...] = ()
    over_budget: bool = False


def transaction_candidates(prepared: CompositionInput, constraint: LayoutProfile | None,
                           max_local_alternatives: int) -> LocalDomains:
    profile = prepared.profile
    coverage = VisualCoverage(prepared.original, profile.tolerances)
    dates = date_assignment_candidates(prepared.segments, prepared.candidates,
        mode=constraint.date_mode if constraint else None, carry_across_pages=profile.carry_date_across_pages)
    amounts = tuple(amount_role_candidates(s, profile, constraint=constraint) for s in prepared.segments)
    if any(not d for d in dates):
        raise OperatorFailure('date_attribution', 'A transaction lacks a supported full-year date owner.')
    if any(not domain for domain in amounts):
        diagnostics = tuple(MonetaryDiagnostic(coverage.span(s.rows[0].row, r.start, r.end),
            EmptyDomainCause.UNSUPPORTED_TRANSACTION_GEOMETRY) for s, domain in zip(prepared.segments, amounts, strict=True)
            if not domain for r in s.rows[0].amounts)
        raise OperatorFailure('amount_role_inference', 'A transaction lacks a supported monetary geometry.', monetary_diagnostics=diagnostics)
    if any(len(d.roles) > max_local_alternatives for d in prepared.monetary_domains):
        return LocalDomains(dates, amounts, (), over_budget=True)
    output, refusals, decisions = [], [], []
    for index, (date_domain, amount_domain) in enumerate(zip(dates, amounts, strict=True)):
        choices = []
        for dated in date_domain:
            for roles in amount_domain:
                segment = dated.segment
                date_source = coverage.span(dated.source_row.row, dated.candidate.start, dated.candidate.end)
                try:
                    direction = infer_direction(segment, roles, profile.amount_mode, prepared.candidates,
                        profile.tolerances, carry_across_pages=profile.carry_date_across_pages)
                    description = description_intervals(segment,
                        dated.candidate if dated.source_row.position == segment.position else None,
                        reject_balance_heading=direction.basis != 'signed_group_subtotal')
                    date_words = {i for d in segment.rows[0].dates for i in range(d.start, d.end)}
                    if not any(row is not segment.rows[0].row or i not in date_words
                               for row, begin, end in description for i in range(begin, end)):
                        raise OperatorFailure('transaction_segmentation', 'Date tokens cannot supply a transaction description.')
                except OperatorFailure as error:
                    refusals.append(error.capability)
                    decisions.append(RoleDecision(coverage.span(segment.rows[0].row, roles.movement.start, roles.movement.end),
                        FinancialRole.MOVEMENT, 'transaction_segment', False, error.capability, (date_source,)))
                    continue
                witnesses = (date_source, *canonical_sources(tuple(coverage.span(row, begin, end) for row, begin, end in description)))
                evidence = tuple(RoleDecision(coverage.span(segment.rows[0].row, region.start, region.end),
                    role, 'transaction_segment', True, 'dated_described_movement' if role == FinancialRole.MOVEMENT
                    else 'distinct_region_in_dated_described_segment', witnesses)
                    for region, role in ((roles.movement, FinancialRole.MOVEMENT),
                        (roles.running_balance, FinancialRole.RUNNING_BALANCE)) if region is not None)
                decisions.extend(evidence)
                choices.append(TransactionCandidate(dated, replace(roles, role_evidence=evidence), direction, date_source,
                    tuple(MonetaryAssignment(d.source, d.role, index) for d in evidence)))
                if len(choices) > max_local_alternatives:
                    return LocalDomains(dates, amounts, (), over_budget=True)
        output.append(tuple(choices))
    decisions = tuple(sorted(set(decisions), key=lambda d: (d.source.page, d.source.row, d.source.word_start,
        d.role, d.generator, d.admitted, d.rule,
        tuple((s.region_id, s.basis, s.page, s.row, s.word_start, s.word_end) for s in d.witnesses))))
    if any(not choices for choices in output):
        diagnostics = tuple(MonetaryDiagnostic(d.source, EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE
            if d.rule == 'monetary_role_domain_empty' else EmptyDomainCause.UNSUPPORTED_TRANSACTION_GEOMETRY, (d,))
                            for d in decisions if not d.admitted)
        raise OperatorFailure(sorted(set(refusals))[0], 'A transaction lacks its required structural fields.', monetary_diagnostics=diagnostics)
    return LocalDomains(dates, amounts, tuple(output), tuple(refusals), decisions)
