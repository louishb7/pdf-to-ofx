"""Reject invalid data and reconcile using exact Decimal arithmetic."""

from datetime import date
from decimal import Context, Decimal, localcontext

from pdf_to_ofx.domain.errors import StatementValidationError
from pdf_to_ofx.domain.evidence import EvidenceReport, EvidenceStatus
from pdf_to_ofx.domain.models import BalanceCheckpoint, Chronology, Statement, Transaction
from pdf_to_ofx.domain.currency import Currency
from pdf_to_ofx.validation.checkpoints import check_checkpoints


def _validate_money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StatementValidationError(f"{field} must be a finite Decimal.")
    _, digits, exponent = value.as_tuple()
    if exponent < -2 and any(digits[exponent + 2:]):
        raise StatementValidationError(f"{field} must have exact cent precision.")


def validate_domain(statement: Statement) -> None:
    """Validate normalized values without assuming a document chronology."""
    if not isinstance(statement, Statement):
        raise StatementValidationError("Expected a normalized Statement.")
    if not isinstance(statement.layout_id, str) or not statement.layout_id.strip():
        raise StatementValidationError("Statement structural layout is required.")
    if type(statement.period_start) is not date or type(statement.period_end) is not date:
        raise StatementValidationError("Statement period must contain calendar dates.")
    if statement.period_start > statement.period_end:
        raise StatementValidationError("Statement period is reversed.")
    if not isinstance(statement.transactions, tuple) or not statement.transactions:
        raise StatementValidationError("Statement must contain an immutable transaction list.")
    if (not isinstance(statement.chronology, Chronology)
            or type(statement.running_balances_required) is not bool):
        raise StatementValidationError("Invalid structural validation declarations.")
    # Currency is not arithmetic: all values already share the statement scope.
    if statement.currency is not None and not isinstance(statement.currency, Currency):
        raise StatementValidationError("Statement currency must be an explicit Currency.")
    for index, transaction in enumerate(statement.transactions, start=1):
        if not isinstance(transaction, Transaction):
            raise StatementValidationError(f"Transaction {index} is not a Transaction.")
        if type(transaction.posting_date) is not date:
            raise StatementValidationError(f"Transaction {index} has an invalid date.")
        if not statement.period_start <= transaction.posting_date <= statement.period_end:
            raise StatementValidationError(f"Transaction {index} falls outside the period.")
        if not isinstance(transaction.description, str) or not transaction.description.strip():
            raise StatementValidationError(f"Transaction {index} has no description.")
        _validate_money(transaction.amount, f"Transaction {index} amount")
        if transaction.balance_after is not None:
            _validate_money(transaction.balance_after, f"Transaction {index} balance")
    for value, label in ((statement.opening_balance, "Opening balance"),
                         (statement.closing_balance, "Closing balance")):
        if value is not None:
            _validate_money(value, label)
    if not isinstance(statement.checkpoints, tuple):
        raise StatementValidationError("Checkpoints must be immutable.")
    for point in statement.checkpoints:
        if not isinstance(point, BalanceCheckpoint):
            raise StatementValidationError("Invalid normalized checkpoint.")
        _validate_money(point.balance, "Checkpoint balance")
        if (type(point.after) is not int or not 0 <= point.after <= len(statement.transactions)
                or point.kind not in {"daily_balance", "sparse_checkpoint"}):
            raise StatementValidationError("Invalid economic checkpoint boundary or role.")


def validate_statement(statement: Statement) -> EvidenceReport:
    """Check available financial evidence; unavailable controls are not failures.

    A parser may declare its current ascending document sequence as economic
    order and require complete running balances. Neither depends on identity.
    Sparse checkpoints reconcile the movements between them; they do not invent
    balances for transactions which lack a source balance.
    """
    validate_domain(statement)
    ordered = statement.chronology != Chronology.UNDECLARED
    economic = statement.transactions[::-1] if statement.chronology == Chronology.DESCENDING else statement.transactions
    if ordered and any(a.posting_date > b.posting_date
                       for a, b in zip(economic, economic[1:])):
        raise StatementValidationError("Declared economic order disagrees with posting-date order.")
    if statement.running_balances_required and any(t.balance_after is None for t in statement.transactions):
        raise StatementValidationError("The structural profile requires running balances for every transaction.")
    values = [t.amount for t in statement.transactions]
    values.extend(t.balance_after for t in statement.transactions if t.balance_after is not None)
    values.extend(v for v in (statement.opening_balance, statement.closing_balance) if v is not None)
    # Allow all input digits and carries, independent of the caller's context.
    precision = max(value.adjusted() for value in values) - min(
        value.as_tuple().exponent for value in values
    ) + len(str(len(values))) + 2
    with localcontext(Context(prec=max(28, precision))):
        links = 0
        first_verified = False
        if ordered:
            previous_balance = statement.opening_balance
            pending = Decimal("0.00")
            for index, transaction in enumerate(economic, start=1):
                pending += transaction.amount
                if transaction.balance_after is None:
                    continue
                if previous_balance is not None:
                    if previous_balance + pending != transaction.balance_after:
                        raise StatementValidationError(f"Running balance mismatch at transaction {index}.")
                    links += 1
                    first_verified |= index == 1 and statement.opening_balance is not None
                previous_balance = transaction.balance_after
                pending = Decimal("0.00")
            if (any(t.balance_after is not None for t in statement.transactions)
                    and previous_balance is not None and statement.closing_balance is not None
                    and previous_balance + pending != statement.closing_balance):
                raise StatementValidationError("Last running balance differs from closing balance.")
        if statement.opening_balance is not None and statement.closing_balance is not None:
            expected = statement.opening_balance + sum(
                (transaction.amount for transaction in statement.transactions), Decimal("0.00")
            )
            if expected != statement.closing_balance:
                raise StatementValidationError(
                    "Opening balance plus transactions differs from closing balance."
                )
    verified = EvidenceStatus.VERIFIED
    unavailable = EvidenceStatus.NOT_AVAILABLE
    points = list(statement.checkpoints)
    if points:
        points.extend(BalanceCheckpoint(i + 1, t.balance_after, "running_balance")
                      for i, t in enumerate(economic) if t.balance_after is not None)
        if statement.opening_balance is not None:
            points.append(BalanceCheckpoint(0, statement.opening_balance, "opening_balance"))
        if statement.closing_balance is not None:
            points.append(BalanceCheckpoint(len(economic), statement.closing_balance, "closing_balance"))
    checkpoint_links = check_checkpoints(tuple(t.amount for t in economic), tuple(points)) if ordered and points else 0
    return EvidenceReport(
        domain_valid=True, economic_order_verified=verified if ordered else unavailable,
        running_balance_verified=verified if links else unavailable,
        running_balance_links=links,
        first_movement_verified=verified if first_verified else unavailable,
        opening_closing_reconciled=verified if statement.opening_balance is not None
            and statement.closing_balance is not None else unavailable,
        checkpoints_verified=verified if checkpoint_links else unavailable,
        checkpoint_links=checkpoint_links,
    )
