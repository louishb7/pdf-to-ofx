"""Bounded local hypotheses over the existing operators; no financial ranking."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pdf_to_ofx.domain.errors import (
    AmbiguousStatementError, CurrencyConflictError, FinancialCoverageError, RecognizedInvalidStatementError,
    StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus, InferenceDiagnostic, Interpretation, MonetaryAssignment, SourceSpan
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.generic.composition import (
    bind_declarations, materialize_composition, prepare_composition, resolve_hypotheses, structural_key,
)
from pdf_to_ofx.generic.operators.amounts import AmountRoles, MonetaryDomain
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.chronology import chronology_candidates
from pdf_to_ofx.generic.operators.dates import DatedSegment
from pdf_to_ofx.generic.operators.directions import DirectionEvidence
from pdf_to_ofx.generic.operators.local import transaction_candidates
from pdf_to_ofx.generic.operators.scopes import FinancialScope
from pdf_to_ofx.generic.operators.transactions import TransactionSegment
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import BalanceMode, LayoutProfile
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.validation.checkpoints import ConstraintViolation
from pdf_to_ofx.generic.constraints import constrain_hypothesis, evaluate_hypothesis, observe_constraint_facts
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.search import SearchLedger

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
        return structural_key(self)


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
    stage = "geometry"
    try:
        geometry = observe_geometry(document, profile, tolerances)
        stage = "structural_composition"
        prepared = prepare_composition(document, geometry, context=context, legacy_context=False)
        if scope_candidates and scope_candidates[0].region != prepared.scope.region:
            raise OperatorFailure("scope_boundary_unknown", "The supplied scope differs from the complete analyzed region.")
        stage = "local_candidates"
        local = transaction_candidates(prepared, profile, budget.max_local_alternatives)
        dates, amounts = local.dates, local.amounts
        facts = observe_constraint_facts(prepared)
    except CurrencyConflictError as error:
        return InferenceResult(AnalysisStatus.INVALID, None, "Contradictory currencies were observed in one financial scope.",
                               blocking_capabilities=(error.capability,))
    except FinancialCoverageError:
        return InferenceResult(AnalysisStatus.INVALID, None, "Financial regions lack complete ownership.", blocking_capabilities=("financial_coverage",))
    except StatementValidationError:
        return InferenceResult(AnalysisStatus.INVALID, None, "Financial declarations contradict their supplied constraints.",
                               blocking_capabilities=("financial_declarations",))
    except StatementParseError as error:
        capability = error.capability if isinstance(error, OperatorFailure) else "visual_structure"
        status = AnalysisStatus.INVALID if capability == "pagination_consistency" else AnalysisStatus.UNSUPPORTED
        return InferenceResult(status, None, str(error), blocking_capabilities=(capability,),
            failure_stage=error.stage or stage if isinstance(error, OperatorFailure) else stage,
            monetary_diagnostics=error.monetary_diagnostics if isinstance(error, OperatorFailure) else ())
    if local.over_budget:
        return InferenceResult(AnalysisStatus.AMBIGUOUS, None, "Local hypothesis budget exceeded.", blocking_capabilities=("search_budget",), budget_exhausted=True)
    orders = chronology_candidates(prepared, amounts)
    fixed_dates = tuple(d[0].candidate.value for d in dates) if all(len(d) == 1 for d in dates) else None
    survivors: list[tuple[StructuralHypothesis, Interpretation]] = []
    ledger = SearchLedger(budget.max_hypotheses)
    for name in local.refusals:
        ledger.reject(name, capability_missing=True)

    def search(hypothesis: StructuralHypothesis, parent: int | None = None,
               choices: tuple[MonetaryAssignment, ...] = (), date_source: SourceSpan | None = None) -> None:
        state = ledger.open(parent, hypothesis.chronology, choices, date_source, hypothesis.complete)
        if state is None:
            return
        try:
            checked = evaluate_hypothesis(prepared, hypothesis, fixed_dates=fixed_dates, facts=facts)
            if hypothesis.complete:
                result = materialize_composition(bind_declarations(prepared, hypothesis.monetary_assignments), hypothesis.date_assignments, hypothesis.monetary_roles,
                    hypothesis.directions, hypothesis.chronology, checkpoints=checked.checkpoints, allow_checkpoints=True)
                survivors.append((hypothesis, result))
                ledger.survive(state, checked.controls)
                return
            if len(hypothesis.monetary_assignments) < len(prepared.monetary_domains):
                domain = prepared.monetary_domains[len(hypothesis.monetary_assignments)]
                for role in domain.roles[::-1] if reverse_candidates else domain.roles:
                    assignment = MonetaryAssignment(domain.source, role, None)
                    search(replace(hypothesis, monetary_assignments=(*hypothesis.monetary_assignments,
                        assignment)), state, (assignment,))
                return
            index = len(hypothesis.date_assignments)
            alternatives = local.alternatives[index]
            for choice in alternatives[::-1] if reverse_candidates else alternatives:
                search(replace(hypothesis, date_assignments=(*hypothesis.date_assignments, choice.dated),
                    monetary_roles=(*hypothesis.monetary_roles, choice.roles), directions=(*hypothesis.directions, choice.direction)),
                    state, choice.assignments, choice.date_source)
        except (ConstraintViolation, FinancialCoverageError, RecognizedInvalidStatementError, OperatorFailure) as error:
            name = error.constraint if isinstance(error, ConstraintViolation) else error.capability if isinstance(error, OperatorFailure) else "financial_coverage" if isinstance(error, FinancialCoverageError) else "financial_reconciliation"
            ledger.reject(name, state=state, capability_missing=isinstance(error, OperatorFailure))
    for order in orders[::-1] if reverse_candidates else orders:
        search(StructuralHypothesis(prepared.scope, prepared.segments, (), (), (), order,
                                   monetary_domains=prepared.monetary_domains))
    details = dict(candidate_hypotheses=ledger.completed, explored_hypotheses=len(ledger.steps),
        pruned_constraints=tuple(sorted(ledger.pruned.items())), hypotheses=tuple(h for h, _ in survivors),
        role_decisions=(*prepared.role_evidence, *(d for domain in prepared.monetary_domains for d in domain.decisions), *local.decisions),
        search_trace=tuple(ledger.steps))
    if ledger.exhausted:
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
    if ledger.unsupported:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "A required local structural capability is unresolved.",
                               blocking_capabilities=tuple(sorted(ledger.unsupported)), **details)
    if ledger.pruned and set(ledger.pruned) <= {"chronology_dates"}:
        return InferenceResult(AnalysisStatus.UNSUPPORTED, None, "Economic chronology lacks verifiable checkpoints.",
                               blocking_capabilities=("chronology_inference",), **details)
    return InferenceResult(AnalysisStatus.INVALID, None, "Supported structural candidates contradict hard constraints.",
                           blocking_capabilities=tuple(sorted(ledger.pruned)), **details)
