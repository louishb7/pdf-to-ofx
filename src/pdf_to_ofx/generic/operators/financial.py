"""Available financial controls are exact constraints, never selection scores."""

from dataclasses import replace
from decimal import Context, Decimal, localcontext

from pdf_to_ofx.domain.errors import RecognizedInvalidStatementError, StatementValidationError
from pdf_to_ofx.domain.evidence import EvidenceReport, EvidenceStatus
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.transactions import RowCandidate
from pdf_to_ofx.generic.profile import BalanceMode
from pdf_to_ofx.validation.statement import validate_statement


def _sum(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0.00")
    precision = max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values)
    with localcontext(Context(prec=max(28, precision + len(str(len(values))) + 2))):
        return sum(values, Decimal("0.00"))


def financial_evidence(statement: Statement, candidates: tuple[RowCandidate, ...],
                       date_groups: tuple[int, ...], direction_groups: tuple[int | None, ...],
                       totals: dict[str, Decimal], balance_mode: BalanceMode) -> EvidenceReport:
    daily = []
    subtotals = []
    for candidate in candidates:
        if candidate.kind == "flow":
            indexes = [i for i, group in enumerate(direction_groups) if group == candidate.position]
            if not indexes:
                raise OperatorFailure("financial_evidence", "A flow group contains no transactions.")
            subtotals.append(_sum([statement.transactions[i].amount for i in indexes]) == candidate.amounts[0].money.amount)
        elif candidate.kind == "date_heading" and candidate.amounts:
            indexes = [i for i, group in enumerate(date_groups) if group == candidate.position]
            if not indexes:
                raise OperatorFailure("financial_evidence", "A date group contains no transactions.")
            daily.append(statement.transactions[indexes[-1]].balance_after == candidate.amounts[0].money.amount)
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
        daily_balances_verified=EvidenceStatus.VERIFIED if daily else EvidenceStatus.NOT_AVAILABLE,
        credit_total_verified=EvidenceStatus.VERIFIED if "credits" in totals else EvidenceStatus.NOT_AVAILABLE,
        debit_total_verified=EvidenceStatus.VERIFIED if "debits" in totals else EvidenceStatus.NOT_AVAILABLE,
    )
