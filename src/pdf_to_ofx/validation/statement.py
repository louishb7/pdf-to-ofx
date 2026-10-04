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
    for value, label in ((statement.opening_balance, "Opening balance"),
                         (statement.closing_balance, "Closing balance")):
        if value is not None:
            _validate_money(value, label)
    if statement.opening_balance is not None and statement.closing_balance is not None:
        values = [statement.opening_balance, statement.closing_balance,
                  *(transaction.amount for transaction in statement.transactions)]
        # Allow all input digits and possible carries. A caller's Decimal context
        # must not round away a financial mismatch, even for very large amounts.
        precision = max(value.adjusted() for value in values) - min(
            value.as_tuple().exponent for value in values
        ) + len(str(len(values))) + 2
        with localcontext(Context(prec=max(28, precision))):
            expected = statement.opening_balance + sum(
                (transaction.amount for transaction in statement.transactions), Decimal("0.00")
            )
            if expected != statement.closing_balance:
                raise StatementValidationError(
                    "Opening balance plus transactions differs from closing balance."
                )
