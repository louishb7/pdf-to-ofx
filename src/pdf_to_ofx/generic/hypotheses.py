"""Bounded local hypotheses over the existing operators; no financial ranking."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.errors import (
    AmbiguousStatementError, FinancialCoverageError, RecognizedInvalidStatementError,
    StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus, InferenceDiagnostic, Interpretation, MonetaryAssignment
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.generic.composition import (
    bind_declarations, materialize_composition, prepare_composition, resolve_hypotheses, source_key,
)
from pdf_to_ofx.generic.operators.amounts import AmountRoles, MonetaryDomain, amount_role_candidates
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.chronology import chronology_candidates
from pdf_to_ofx.generic.operators.dates import DatedSegment, date_assignment_candidates
from pdf_to_ofx.generic.operators.directions import DirectionEvidence, infer_direction
from pdf_to_ofx.generic.operators.scopes import FinancialScope
from pdf_to_ofx.generic.operators.transactions import TransactionSegment, description_intervals
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import BalanceMode, LayoutProfile
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.validation.checkpoints import ConstraintViolation
from pdf_to_ofx.generic.constraints import constrain_hypothesis
from pdf_to_ofx.generic.operators.geometry import observe_geometry

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
    monetary_domains: tuple[MonetaryDomain, ...] = ()
    monetary_assignments: tuple[MonetaryAssignment, ...] = ()

    @property
    def complete(self) -> bool:
        return (len(self.date_assignments) == len(self.monetary_roles) == len(self.directions)
                == len(self.transaction_segments) and len(self.monetary_assignments) == len(self.monetary_domains))

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
        return self.scope.region.region_id, tuple(output), economic, self.monetary_assignments


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
        geometry = observe_geometry(document, profile, tolerances)
        prepared = prepare_composition(document, geometry, context=context, legacy_context=False)
        if scope_candidates and scope_candidates[0].region != prepared.scope.region:
            raise OperatorFailure("scope_boundary_unknown", "The supplied scope differs from the complete analyzed region.")
        dates = date_assignment_candidates(prepared.segments, prepared.candidates,
            mode=profile.date_mode if profile else None, carry_across_pages=geometry.carry_date_across_pages)
        amounts = tuple(amount_role_candidates(s, geometry, constraint=profile) for s in prepared.segments)
        if any(not domain.roles for domain in prepared.monetary_domains):
            raise OperatorFailure("monetary_role_domain_empty", "A monetary occurrence has no supported role.")
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
    if (any(len(d) * len(a) > budget.max_local_alternatives for d, a in zip(dates, amounts, strict=True))
            or any(len(domain.roles) > budget.max_local_alternatives for domain in prepared.monetary_domains)):
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Local hypothesis budget exceeded.", blocking_capabilities=("search_budget",), budget_exhausted=True)
    orders = chronology_candidates(prepared, amounts)
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
                result = materialize_composition(bind_declarations(prepared, hypothesis.monetary_assignments), hypothesis.date_assignments, hypothesis.monetary_roles,
                    hypothesis.directions, hypothesis.chronology, checkpoints=points, allow_checkpoints=True)
                survivors.append((hypothesis, result))
                return
            if len(hypothesis.monetary_assignments) < len(prepared.monetary_domains):
                domain = prepared.monetary_domains[len(hypothesis.monetary_assignments)]
                for role in domain.roles[::-1] if reverse_candidates else domain.roles:
                    search(replace(hypothesis, monetary_assignments=(*hypothesis.monetary_assignments,
                        MonetaryAssignment(domain.source, role, None))))
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
        search(StructuralHypothesis(prepared.scope, prepared.segments, (), (), (), order,
                                   monetary_domains=prepared.monetary_domains))
    details = dict(candidate_hypotheses=completed, explored_hypotheses=visited, pruned_constraints=tuple(sorted(pruned.items())),
                   hypotheses=tuple(h for h, _ in survivors))
    if exhausted:
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Global hypothesis budget exceeded.",
            blocking_capabilities=("search_budget",), budget_exhausted=True, **details)
    if survivors:
        try:
            # Evidence strength must not rank material alternatives. Uniqueness
            # precedes the minimum-evidence policy, including weaker survivors.
            interpretation = resolve_hypotheses(tuple(result for _, result in survivors))
        except AmbiguousStatementError:
            return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Material financial hypotheses remain indistinguishable.", **details)
        if not interpretation.evidence.has_financial_support:
            return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "No independent financial control verifies the surviving interpretation.",
                diagnostic=InferenceDiagnostic.INSUFFICIENT_EVIDENCE, **details)
        selected = next(h for h, result in survivors if result is interpretation)
        head = selected.monetary_roles[0]
        column = selected.transaction_segments[0].rows[0].amounts.index(head.movement)
        portable = replace(geometry, movement_column=column, balance_column=1-column if geometry.balance_mode == BalanceMode.RUNNING else None)
        return InferenceResult(AnalysisStatus.SUCCESS, portable, "A unique covered hypothesis satisfies independent financial controls.", interpretation, **details)
    if unsupported:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "A required local structural capability is unresolved.",
                               blocking_capabilities=tuple(sorted(unsupported)), **details)
    if pruned and set(pruned) <= {"chronology_dates"}:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "Economic chronology lacks verifiable checkpoints.",
                               blocking_capabilities=("chronology_inference",), **details)
    return InferenceResult(AnalysisStatus.INVALID, None, "Supported structural candidates contradict hard constraints.",
                           blocking_capabilities=tuple(sorted(pruned)), **details)
