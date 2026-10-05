"""Monetary observations and small role domains authorized by structure.

No monetary value is inspected here. A recurring transaction column can anchor
an isolated cell only at a demonstrable scope or date-group frontier. The search
must still select that column as balance, and choose a coherent economic order.
"""

from dataclasses import dataclass, replace

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.generic.operators.amounts import (
    EmptyDomainCause, MonetaryDiagnostic, MonetaryDomain, MonetaryObservation, RoleDecision,
)
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.dates import date_assignment_candidates
from pdf_to_ofx.generic.operators.transactions import RowCandidate, TransactionSegment, description_intervals
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage


def observe_money(candidates: tuple[RowCandidate, ...], coverage: VisualCoverage) -> tuple[MonetaryObservation, ...]:
    return tuple(MonetaryObservation(coverage.span(c.row, r.start, r.end), r, c.position)
                 for c in candidates for r in c.amounts)


@dataclass(frozen=True, slots=True)
class ControlRelations:
    column: int | None
    cut: int
    scope_frontier: bool
    group_frontier: bool
    witnesses: tuple[SourceSpan, ...]
    refusal: EmptyDomainCause | None = None


def _relations(observation: MonetaryObservation, candidates: tuple[RowCandidate, ...],
               segments: tuple[TransactionSegment, ...], coverage: VisualCoverage,
               profile: LayoutProfile) -> ControlRelations:
    row = candidates[observation.row_position]
    cut = sum(s.position < row.position for s in segments)
    unavailable = ControlRelations(None, cut, False, False, (), EmptyDomainCause.NO_STRUCTURAL_OWNER)
    if row.kind != "monetary_only":
        return replace(unavailable, refusal=EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE
            if row.kind in {"flow", "checkpoint"} or row.row.text.casefold().startswith("subtotal")
            else EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT)
    if len(segments) < 2 or any(len(s.rows[0].amounts) != 2 for s in segments):
        return unavailable
    dates = date_assignment_candidates(segments, candidates, carry_across_pages=profile.carry_date_across_pages)
    for domain in dates:
        complete = False
        for dated in domain:
            try:
                description_intervals(dated.segment,
                    dated.candidate if dated.source_row.position == dated.segment.position else None)
                complete = True
            except OperatorFailure:
                continue
        if not complete:
            return replace(unavailable, refusal=EmptyDomainCause.UNSUPPORTED_TRANSACTION_GEOMETRY)
    columns = tuple(tuple(s.rows[0].amounts[column] for s in segments) for column in (0, 1))
    if max(r.x1 for r in columns[0]) + profile.tolerances.row_y >= min(r.x0 for r in columns[1]):
        return replace(unavailable, refusal=EmptyDomainCause.UNSUPPORTED_TRANSACTION_GEOMETRY)
    aligned = []
    for column, regions in enumerate(columns):
        if any(max(edges) - min(edges) <= profile.tolerances.row_y
               for edges in ((observation.region.x0, *(r.x0 for r in regions)),
                             (observation.region.x1, *(r.x1 for r in regions)))):
            aligned.append(column)
    if len(aligned) != 1:
        return replace(unavailable, refusal=EmptyDomainCause.CONFLICTING_STRUCTURAL_EVIDENCE if aligned else
                       EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT)
    before = segments[cut - 1].rows[-1] if cut else None
    after = segments[cut].rows[0] if cut < len(segments) else None
    # A nearby transaction is necessary, but only the recurring column plus a
    # frontier authorizes the control. Proximity alone never creates a role.
    adjacent = any(other is not None and other.row.page == row.row.page and
                   min(abs(row.row.words[0].top - other.row.words[0].bottom),
                       abs(other.row.words[0].top - row.row.words[0].bottom)) <= profile.tolerances.summary_y
                   for other in (before, after))
    frontier = cut in (0, len(segments)) and adjacent
    headings = tuple(c for c in candidates if c.kind == "date_heading" and
                     before is not None and after is not None and before.position < c.position < after.position)
    grouped = bool(headings) and adjacent and 0 < cut < len(segments)
    column = aligned[0]
    witnesses = tuple(coverage.span(s.rows[0].row, r.start, r.end)
                      for s, r in zip(segments, columns[column], strict=True))
    witnesses += tuple(coverage.span(c.row) for c in headings)
    return ControlRelations(column, cut, frontier, grouped, witnesses,
                            None if frontier or grouped else EmptyDomainCause.UNSUPPORTED_CONTROL_STRUCTURE)


def scope_boundary_candidates(observation: MonetaryObservation, relations: ControlRelations) -> tuple[RoleDecision, ...]:
    return tuple(RoleDecision(observation.source, role, "scope_boundary", relations.scope_frontier,
        "recurring_column_at_scope_frontier" if relations.scope_frontier else "scope_frontier_not_demonstrated",
        relations.witnesses) for role in (FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE))


def group_boundary_candidates(observation: MonetaryObservation, relations: ControlRelations) -> tuple[RoleDecision, ...]:
    return (RoleDecision(observation.source, FinancialRole.SPARSE_CHECKPOINT, "group_boundary", relations.group_frontier,
        "recurring_column_at_date_group_cut" if relations.group_frontier else "group_cut_not_demonstrated",
        relations.witnesses),)


RELATIONAL_GENERATORS = (scope_boundary_candidates, group_boundary_candidates)


def generate_relational_domains(domains: tuple[MonetaryDomain, ...], observations: tuple[MonetaryObservation, ...],
                                candidates: tuple[RowCandidate, ...], segments: tuple[TransactionSegment, ...],
                                coverage: VisualCoverage, profile: LayoutProfile) -> tuple[MonetaryDomain, ...]:
    observed = {o.source: o for o in observations}
    output = []
    diagnostics = []
    for domain in domains:
        if domain.roles:
            output.append(domain)
            continue
        observation = observed[domain.source]
        relations = _relations(observation, candidates, segments, coverage, profile)
        decisions = tuple(sorted((decision for generator in RELATIONAL_GENERATORS
            for decision in generator(observation, relations)), key=lambda d: (d.generator, d.role, d.rule)))
        roles = tuple(sorted({d.role for d in decisions if d.admitted}))
        output.append(replace(domain, roles=roles, decisions=decisions,
                              boundary=relations.cut if roles else None, column=relations.column if roles else None))
        if not roles:
            diagnostics.append(MonetaryDiagnostic(domain.source,
                relations.refusal or EmptyDomainCause.ROLE_EVIDENCE_INSUFFICIENT, decisions))
    if diagnostics:
        raise OperatorFailure("monetary_role_domain_empty", "Observed monetary regions have no structurally authorized role.",
                              monetary_diagnostics=tuple(sorted(diagnostics, key=lambda d: (d.source.page, d.source.row, d.source.word_start))))
    return tuple(sorted(output, key=lambda d: (d.source.page, d.source.row, d.source.word_start)))
