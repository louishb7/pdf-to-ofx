"""Bounded local hypotheses over the existing operators; no financial ranking."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.errors import (
    AmbiguousStatementError, FinancialCoverageError, RecognizedInvalidStatementError,
    StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus, Interpretation
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology
from pdf_to_ofx.generic.composition import (
    CompositionInput, materialize_composition, prepare_composition, resolve_hypotheses, source_key,
)
from pdf_to_ofx.generic.operators.amounts import AmountRoles, amount_role_candidates, infer_control_roles
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.dates import DatedSegment, date_assignment_candidates
from pdf_to_ofx.generic.operators.directions import DirectionEvidence, infer_direction
from pdf_to_ofx.generic.operators.scopes import FinancialScope
from pdf_to_ofx.generic.operators.transactions import TransactionSegment, description_intervals, semantic_candidates
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.validation.checkpoints import ConstraintViolation, _sum, check_checkpoints

if TYPE_CHECKING:
    from pdf_to_ofx.generic.inference import InferenceResult


@dataclass(frozen=True, slots=True)
class SearchBudget:
    max_local_alternatives: int = 4
    max_hypotheses: int = 128

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in (self.max_local_alternatives, self.max_hypotheses)):
            raise ValueError("Search budgets require positive integer limits.")


@dataclass(frozen=True, slots=True)
class StructuralHypothesis:
    scope: FinancialScope
    transaction_segments: tuple[TransactionSegment, ...]
    date_assignments: tuple[DatedSegment, ...]
    monetary_roles: tuple[AmountRoles, ...]
    directions: tuple[DirectionEvidence, ...]
    chronology: Chronology

    @property
    def complete(self) -> bool:
        return (len(self.date_assignments) == len(self.monetary_roles) == len(self.directions)
                == len(self.transaction_segments))

    def semantic_key(self) -> tuple:
        """Material fields and original token ownership, without segment syntax."""
        references = {id(row): span for row, span in zip(self.scope.rows, self.scope.region.spans, strict=True)}

        def tokens(row, begin, end):
            return replace(references[id(row)], word_start=begin, word_end=end)

        output = []
        for dated, roles, direction in zip(self.date_assignments, self.monetary_roles, self.directions, strict=True):
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
        economic = None if self.chronology == Chronology.UNKNOWN else order[::-1] if self.chronology == Chronology.DESCENDING else order
        return self.scope.region.region_id, tuple(output), economic


def _geometry(document: ExtractedDocument, supplied: LayoutProfile | None, tolerances: Tolerances) -> LayoutProfile:
    if supplied:
        return supplied
    from pdf_to_ofx.generic.grouped import infer_grouped_profile
    from pdf_to_ofx.generic.inference import _contact_footer_rows
    rows = reconstruct_rows(document, tolerances)
    framed = infer_grouped_profile(rows, tolerances)
    if framed:
        return framed
    base = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING,
                         footer_rows=_contact_footer_rows(rows), tolerances=tolerances)
    candidates = semantic_candidates(rows, base)
    start = next((c.position for c in candidates if c.dates and
                  (c.kind in {"date_heading", "flow"} or c.kind == "content" and c.amounts)), len(candidates))
    movements = [c for c in candidates[start:] if c.kind == "content" and c.amounts]
    grouped = any(c.kind in {"date_heading", "flow"} and c.dates for c in candidates[start:])
    flow = any(c.kind == "flow" for c in candidates[start:])
    markers = any(r.money.marker for c in movements for r in c.amounts)
    mode = AmountMode.GROUP_SUBTOTAL if flow and all(len(c.amounts) == 1 and
        not c.amounts[0].money.explicit_sign and not c.amounts[0].money.marker for c in movements) else (
            AmountMode.CREDIT_DEBIT_MARKER if markers else AmountMode.SIGNED)
    balance = BalanceMode.RUNNING if any(len(c.amounts) == 2 for c in movements) else BalanceMode.ABSENT
    return replace(base, date_mode=DateMode.GROUPED if grouped else DateMode.PER_TRANSACTION,
                   amount_mode=mode, balance_mode=balance, balance_column=1 if balance == BalanceMode.RUNNING else None,
                   carry_date_across_pages=grouped, transaction_left=min((c.row.words[0].x0 for c in movements), default=0)
                   if mode == AmountMode.GROUP_SUBTOTAL else None)


def _checkpoint_candidates(prepared: CompositionInput, hypothesis: StructuralHypothesis,
                           coverage: VisualCoverage, dates: tuple[date, ...] | None) -> tuple[BalanceCheckpoint, ...]:
    count = len(prepared.segments)
    points = []
    context = prepared.context
    for value, boundary, role in ((context.opening_balance, 0, "opening_balance"),
                                  (context.closing_balance, count, "closing_balance")):
        if value is not None:
            source = next((a.source for a in prepared.declarations if a.role.value == role), None)
            points.append(BalanceCheckpoint(boundary, value, role, source))
    for index, (segment, roles) in enumerate(zip(hypothesis.transaction_segments, hypothesis.monetary_roles)):
        if roles.running_balance:
            economic = index if hypothesis.chronology == Chronology.ASCENDING else count - index - 1
            region = roles.running_balance
            points.append(BalanceCheckpoint(economic + 1, region.money.amount, "running_balance",
                coverage.span(segment.rows[0].row, region.start, region.end)))
    for candidate in prepared.candidates:
        if candidate.kind == "checkpoint":
            cut = sum(s.position < candidate.position for s in prepared.segments)
            if hypothesis.chronology == Chronology.DESCENDING:
                cut = count - cut
            region = candidate.amounts[0]
            points.append(BalanceCheckpoint(cut, region.money.amount, "sparse_checkpoint",
                                            coverage.span(candidate.row, region.start, region.end)))
        elif candidate.kind == "date_heading" and candidate.amounts and dates is not None:
            if len(candidate.amounts) != 1:
                raise OperatorFailure("balance_semantics_unknown", "A daily checkpoint needs one exclusive balance region.")
            day = candidate.dates[0].value
            if day not in dates:
                raise ConstraintViolation("daily_checkpoint_date")
            region = candidate.amounts[0]
            points.append(BalanceCheckpoint(sum(d <= day for d in dates), region.money.amount, "daily_balance",
                                            coverage.span(candidate.row, region.start, region.end)))
    return tuple(points)


def constrain_hypothesis(prepared: CompositionInput, hypothesis: StructuralHypothesis,
                         *, fixed_dates: tuple[date, ...] | None = None) -> tuple[BalanceCheckpoint, ...]:
    """Hard constraints act on partial assignments, before a Statement exists."""
    coverage = VisualCoverage(prepared.original, prepared.profile.tolerances)
    owned = {a.source for a in prepared.declarations}
    rows = {id(row) for row in hypothesis.scope.rows}
    count = len(prepared.segments)
    if (hypothesis.scope != prepared.scope or hypothesis.transaction_segments != prepared.segments
            or not isinstance(hypothesis.chronology, Chronology)
            or not len(hypothesis.date_assignments) == len(hypothesis.monetary_roles) == len(hypothesis.directions)
            or len(hypothesis.date_assignments) > count):
        raise ConstraintViolation("scope_and_segment_origin")
    for candidate in prepared.candidates:
        for control in infer_control_roles(candidate, prepared.profile.balance_mode, allow_checkpoints=True):
            source = coverage.span(candidate.row, control.region.start, control.region.end)
            if source not in coverage.expected or source in owned:
                raise ConstraintViolation("exclusive_monetary_ownership")
            owned.add(source)
    values: list[Decimal | None] = [None] * count
    columns = set()
    descriptions = set()
    date_domains = date_assignment_candidates(prepared.segments, prepared.candidates,
                                             carry_across_pages=prepared.profile.carry_date_across_pages)
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
        expected_direction = infer_direction(segment, roles, prepared.profile.amount_mode, prepared.candidates,
            prepared.profile.tolerances, carry_across_pages=prepared.profile.carry_date_across_pages)
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
    points = _checkpoint_candidates(prepared, hypothesis, coverage, dates)
    if hypothesis.chronology == Chronology.UNKNOWN:
        if any(p.kind in {"running_balance", "daily_balance", "sparse_checkpoint"} for p in points):
            raise OperatorFailure("chronology_unknown", "Checkpoint positions require an economic order hypothesis.")
    check_checkpoints(tuple(values), points)
    for candidate in prepared.candidates:
        if candidate.kind != "flow":
            continue
        members = [i for i, s in enumerate(prepared.segments) if s.position > candidate.position and
                   not any(c.kind in {"flow", "date_heading"} and candidate.position < c.position < s.position
                           for c in prepared.candidates)]
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
    if hypothesis.complete and hypothesis.chronology != Chronology.UNKNOWN:
        economic_dates = dates[::-1] if hypothesis.chronology == Chronology.DESCENDING else dates
        if any(a > b for a, b in zip(economic_dates, economic_dates[1:])):
            raise ConstraintViolation("chronology_dates")
    if hypothesis.complete:
        for candidate in prepared.candidates:
            if (candidate.kind == "date_heading" and not candidate.amounts
                    and not any(s.position > candidate.position and not any(
                        c.kind == "date_heading" and candidate.position < c.position < s.position
                        for c in prepared.candidates) for s in prepared.segments)):
                raise OperatorFailure("transaction_segmentation", "A date group contains no transactions.")
    return points


def infer_hypotheses(document: ExtractedDocument, *, profile: LayoutProfile | None = None,
                     context: StatementContext | None = None, tolerances: Tolerances = Tolerances(),
                     budget: SearchBudget = SearchBudget(), reverse_candidates: bool = False,
                     scope_candidates: tuple[FinancialScope, ...] | None = None) -> InferenceResult:
    from pdf_to_ofx.generic.inference import InferenceResult
    if scope_candidates == ():
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "No financial scope is available.",
                               blocking_capabilities=("scope_boundary_unknown",))
    if scope_candidates is not None and len(scope_candidates) != 1:
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Financial scope selection is not unique.",
            blocking_capabilities=("scope_selection",), candidate_hypotheses=len(scope_candidates))
    context = replace(context, bank_id="unknown", account=None) if context else None
    try:
        geometry = _geometry(document, profile, tolerances)
        prepared = prepare_composition(document, geometry, context=context, legacy_context=False)
        if scope_candidates and scope_candidates[0].region != prepared.scope.region:
            raise OperatorFailure("scope_boundary_unknown", "The supplied scope differs from the complete analyzed region.")
        dates = date_assignment_candidates(prepared.segments, prepared.candidates,
            mode=profile.date_mode if profile else None, carry_across_pages=geometry.carry_date_across_pages)
        amounts = tuple(amount_role_candidates(s, geometry, constraint=profile) for s in prepared.segments)
        if any(not domain for domain in dates):
            raise OperatorFailure("date_attribution", "A transaction has no supported full-year date candidate.")
        if any(not domain for domain in amounts):
            raise OperatorFailure("amount_role_inference", "A transaction has no supported monetary-role candidate.")
    except FinancialCoverageError:
        return InferenceResult(AnalysisStatus.INVALID, None, "Financial regions lack complete ownership.", blocking_capabilities=("financial_coverage",))
    except StatementValidationError:
        return InferenceResult(AnalysisStatus.INVALID, None, "Financial declarations contradict their supplied constraints.",
                               blocking_capabilities=("financial_declarations",))
    except StatementParseError as error:
        capability = error.capability if isinstance(error, OperatorFailure) else "visual_structure"
        status = AnalysisStatus.INVALID if capability == "pagination_consistency" else AnalysisStatus.UNSUPPORTED
        return InferenceResult(status, None, "Structural declarations are inconsistent." if status == AnalysisStatus.INVALID
            else "Required structural evidence is unavailable.", blocking_capabilities=(capability,))
    if any(len(d) * len(a) > budget.max_local_alternatives for d, a in zip(dates, amounts, strict=True)):
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Local hypothesis budget exceeded.", blocking_capabilities=("search_budget",), budget_exhausted=True)
    checkpoints_observed = any(c.kind == "checkpoint" or c.kind == "date_heading" and c.amounts for c in prepared.candidates)
    running = sum(any(role.running_balance for role in domain) for domain in amounts)
    has_order_controls = running >= 2 or running and (prepared.context.opening_balance is not None or prepared.context.closing_balance is not None) or checkpoints_observed
    orders = (Chronology.ASCENDING, Chronology.DESCENDING) if has_order_controls else (Chronology.ASCENDING,)
    fixed_dates = tuple(d[0].candidate.value for d in dates) if all(len(d) == 1 for d in dates) else None
    survivors: list[tuple[StructuralHypothesis, Interpretation]] = []
    pruned: dict[str, int] = {}
    unsupported: set[str] = set()
    visited = completed = 0
    exhausted = False

    def search(hypothesis: StructuralHypothesis) -> None:
        nonlocal visited, completed, exhausted
        if exhausted:
            return
        if visited >= budget.max_hypotheses:
            exhausted = True
            return
        visited += 1
        if hypothesis.complete:
            completed += 1
        try:
            points = constrain_hypothesis(prepared, hypothesis, fixed_dates=fixed_dates)
            if hypothesis.complete:
                extra = tuple(p for p in points if p.kind == "sparse_checkpoint" or
                    p.kind == "daily_balance" and any(r.running_balance is None for r in hypothesis.monetary_roles))
                result = materialize_composition(prepared, hypothesis.date_assignments, hypothesis.monetary_roles,
                    hypothesis.directions, hypothesis.chronology, checkpoints=extra, allow_checkpoints=True)
                survivors.append((hypothesis, result))
                return
            index = len(hypothesis.date_assignments)
            alternatives = [(d, a) for d in dates[index] for a in amounts[index]]
            if reverse_candidates:
                alternatives.reverse()
            for dated, roles in alternatives:
                try:
                    direction = infer_direction(dated.segment, roles, geometry.amount_mode, prepared.candidates,
                        geometry.tolerances, carry_across_pages=geometry.carry_date_across_pages)
                except OperatorFailure as error:
                    unsupported.add(error.capability)
                    pruned[error.capability] = pruned.get(error.capability, 0) + 1
                    continue
                search(replace(hypothesis, date_assignments=(*hypothesis.date_assignments, dated),
                    monetary_roles=(*hypothesis.monetary_roles, roles), directions=(*hypothesis.directions, direction)))
        except (ConstraintViolation, FinancialCoverageError, RecognizedInvalidStatementError, OperatorFailure) as error:
            if isinstance(error, OperatorFailure):
                unsupported.add(error.capability)
            name = error.constraint if isinstance(error, ConstraintViolation) else error.capability if isinstance(error, OperatorFailure) else "financial_coverage" if isinstance(error, FinancialCoverageError) else "financial_reconciliation"
            pruned[name] = pruned.get(name, 0) + 1
    for order in orders[::-1] if reverse_candidates else orders:
        search(StructuralHypothesis(prepared.scope, prepared.segments, (), (), (), order))
    details = dict(candidate_hypotheses=completed, explored_hypotheses=visited, pruned_constraints=tuple(sorted(pruned.items())),
                   hypotheses=tuple(h for h, _ in survivors))
    if exhausted:
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Global hypothesis budget exceeded.",
            blocking_capabilities=("search_budget",), budget_exhausted=True, **details)
    supported = tuple(result for _, result in survivors if result.evidence.has_financial_support)
    if supported:
        try:
            interpretation = resolve_hypotheses(supported)
        except AmbiguousStatementError:
            return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Material financial hypotheses remain indistinguishable.", **details)
        selected = next(h for h, result in survivors if result is interpretation)
        head = selected.monetary_roles[0]
        column = selected.transaction_segments[0].rows[0].amounts.index(head.movement)
        portable = replace(geometry, movement_column=column, balance_column=1-column if geometry.balance_mode == BalanceMode.RUNNING else None)
        return InferenceResult(AnalysisStatus.SUCCESS, portable, "A unique covered hypothesis satisfies independent financial controls.", interpretation, **details)
    if survivors:
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "No independent financial control verifies the surviving interpretation.", **details)
    if unsupported:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "A required local structural capability is unresolved.",
                               blocking_capabilities=tuple(sorted(unsupported)), **details)
    if pruned and set(pruned) <= {"chronology_dates"}:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "Economic chronology lacks verifiable checkpoints.",
                               blocking_capabilities=("chronology_inference",), **details)
    return InferenceResult(AnalysisStatus.INVALID, None, "Supported structural candidates contradict hard constraints.",
                           blocking_capabilities=tuple(sorted(pruned)), **details)
