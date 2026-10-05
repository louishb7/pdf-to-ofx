"""Available financial controls are exact constraints, never selection scores."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from decimal import Decimal

from pdf_to_ofx.domain.errors import RecognizedInvalidStatementError, StatementValidationError
from pdf_to_ofx.domain.evidence import EvidenceReport, EvidenceStatus, SourceSpan
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology, Statement
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.transactions import RowCandidate
from pdf_to_ofx.generic.profile import BalanceMode
from pdf_to_ofx.validation.checkpoints import ConstraintViolation, _sum
from pdf_to_ofx.validation.statement import validate_statement


if TYPE_CHECKING:
    from pdf_to_ofx.generic.composition import CompositionInput
    from pdf_to_ofx.generic.hypotheses import StructuralHypothesis
    from pdf_to_ofx.generic.provenance import VisualCoverage


def checkpoint_candidates(prepared: CompositionInput, hypothesis: StructuralHypothesis,
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
    for domain, assignment in zip(prepared.monetary_domains, hypothesis.monetary_assignments):
        if assignment.role.value == "sparse_checkpoint":
            cut = count - domain.boundary if hypothesis.chronology == Chronology.DESCENDING else domain.boundary
            points.append(BalanceCheckpoint(cut, domain.region.money.amount, "sparse_checkpoint", domain.source))
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


def financial_evidence(statement: Statement, candidates: tuple[RowCandidate, ...],
                       date_groups: tuple[int, ...], direction_groups: tuple[int | None, ...],
                       totals: dict[str, Decimal], balance_mode: BalanceMode,
                       *, control_sources: dict[int, SourceSpan] | None = None) -> EvidenceReport:
    daily = []
    checkpoint_daily = False
    subtotals = []
    for candidate in candidates:
        if candidate.kind == "flow":
            indexes = [i for i, group in enumerate(direction_groups) if group == candidate.position]
            if not indexes:
                raise OperatorFailure("financial_evidence", "A flow group contains no transactions.")
            subtotals.append(_sum([statement.transactions[i].amount for i in indexes]) == candidate.amounts[0].money.amount)
        elif candidate.kind == "date_heading" and candidate.amounts:
            sourced = [point for point in statement.checkpoints if point.kind == "daily_balance"
                       and point.source is not None and control_sources is not None
                       and point.source == control_sources.get(candidate.position)]
            if any(point.balance == candidate.amounts[0].money.amount for point in sourced):
                # The central checkpoint validator verifies the economic cut.
                checkpoint_daily = True
                daily.append(True)
                continue
            indexes = [i for i, group in enumerate(date_groups) if group == candidate.position]
            if not indexes:
                raise OperatorFailure("financial_evidence", "A date group contains no transactions.")
            last = indexes[0] if statement.chronology == Chronology.DESCENDING else indexes[-1]
            daily.append(statement.transactions[last].balance_after == candidate.amounts[0].money.amount)
    credits = _sum([t.amount for t in statement.transactions if t.amount >= 0])
    debits = _sum([t.amount for t in statement.transactions if t.amount < 0])
    try:
        if not all(subtotals):
            raise StatementValidationError("Transactions do not reconcile with their declared flow subtotal.")
        if not all(daily):
            raise StatementValidationError("Daily balance differs from its last transaction.")
        if totals.get("credits", credits) != credits or totals.get("debits", debits) != debits:
            raise StatementValidationError("Transactions differ from declared statement credit/debit totals.")
        evidence = validate_statement(statement)
    except StatementValidationError as error:
        raise RecognizedInvalidStatementError(str(error)) from error
    return replace(evidence,
        running_balance_verified=evidence.running_balance_verified if balance_mode == BalanceMode.RUNNING else EvidenceStatus.NOT_APPLICABLE,
        group_subtotals_verified=EvidenceStatus.VERIFIED if subtotals else EvidenceStatus.NOT_APPLICABLE,
        daily_balances_verified=EvidenceStatus.VERIFIED if daily and (
            not checkpoint_daily or evidence.checkpoints_verified == EvidenceStatus.VERIFIED) else EvidenceStatus.NOT_AVAILABLE,
        credit_total_verified=EvidenceStatus.VERIFIED if "credits" in totals else EvidenceStatus.NOT_AVAILABLE,
        debit_total_verified=EvidenceStatus.VERIFIED if "debits" in totals else EvidenceStatus.NOT_AVAILABLE,
    )
