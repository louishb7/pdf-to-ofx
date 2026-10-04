"""Reject invalid data and reconcile using exact Decimal arithmetic."""

from datetime import date
from decimal import Context, Decimal, localcontext

from pdf_to_ofx.domain.errors import StatementValidationError
from pdf_to_ofx.domain.models import Statement, Transaction


def _validate_money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StatementValidationError(f"{field} must be a finite Decimal.")
    _, digits, exponent = value.as_tuple()
    if exponent < -2 and any(digits[exponent + 2:]):
        raise StatementValidationError(f"{field} must have exact cent precision.")


def validate_statement(statement: Statement) -> None:
    if not isinstance(statement, Statement):
        raise StatementValidationError("Expected a normalized Statement.")
    if any(not isinstance(value, str) or not value.strip()
           for value in (statement.bank_id, statement.layout_id)):
        raise StatementValidationError("Statement bank and layout are required.")
    if type(statement.period_start) is not date or type(statement.period_end) is not date:
        raise StatementValidationError("Statement period must contain calendar dates.")
    if statement.period_start > statement.period_end:
        raise StatementValidationError("Statement period is reversed.")
    if not isinstance(statement.transactions, tuple) or not statement.transactions:
        raise StatementValidationError("Statement must contain an immutable transaction list.")
    previous_date = statement.period_start
    for index, transaction in enumerate(statement.transactions, start=1):
        if not isinstance(transaction, Transaction):
            raise StatementValidationError(f"Transaction {index} is not a Transaction.")
        if type(transaction.posting_date) is not date:
            raise StatementValidationError(f"Transaction {index} has an invalid date.")
        if not statement.period_start <= transaction.posting_date <= statement.period_end:
            raise StatementValidationError(f"Transaction {index} falls outside the period.")
        if transaction.posting_date < previous_date:
            raise StatementValidationError("Transactions must be in posting-date order.")
        previous_date = transaction.posting_date
        if not isinstance(transaction.description, str) or not transaction.description.strip():
            raise StatementValidationError(f"Transaction {index} has no description.")
        _validate_money(transaction.amount, f"Transaction {index} amount")
        if transaction.balance_after is not None:
            _validate_money(transaction.balance_after, f"Transaction {index} balance")
    for value, label in ((statement.opening_balance, "Opening balance"),
                         (statement.closing_balance, "Closing balance")):
        if value is not None:
            _validate_money(value, label)
    has_running_balances = any(t.balance_after is not None for t in statement.transactions)
    if has_running_balances or statement.bank_id == "inter":
        if any(t.balance_after is None for t in statement.transactions):
            raise StatementValidationError("Running balances must cover every transaction.")
    values = [t.amount for t in statement.transactions]
    values.extend(t.balance_after for t in statement.transactions if t.balance_after is not None)
    values.extend(v for v in (statement.opening_balance, statement.closing_balance) if v is not None)
    # Allow all input digits and carries, independent of the caller's context.
    precision = max(value.adjusted() for value in values) - min(
        value.as_tuple().exponent for value in values
    ) + len(str(len(values))) + 2
    with localcontext(Context(prec=max(28, precision))):
        if has_running_balances:
            previous_balance = statement.opening_balance
            for index, transaction in enumerate(statement.transactions, start=1):
                if (previous_balance is not None
                        and previous_balance + transaction.amount != transaction.balance_after):
                    raise StatementValidationError(f"Running balance mismatch at transaction {index}.")
                previous_balance = transaction.balance_after
            if statement.closing_balance is not None and previous_balance != statement.closing_balance:
                raise StatementValidationError("Last running balance differs from closing balance.")
        if statement.opening_balance is not None and statement.closing_balance is not None:
            expected = statement.opening_balance + sum(
                (transaction.amount for transaction in statement.transactions), Decimal("0.00")
            )
            if expected != statement.closing_balance:
                raise StatementValidationError(
                    "Opening balance plus transactions differs from closing balance."
                )
