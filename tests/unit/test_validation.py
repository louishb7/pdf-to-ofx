from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import Decimal, localcontext

import pytest

from pdf_to_ofx.domain.errors import StatementValidationError
from pdf_to_ofx.domain.models import Statement, Transaction
from pdf_to_ofx.validation.statement import validate_statement


def test_valid_statement_passes(statement: Statement) -> None:
    validate_statement(statement)


def test_domain_values_are_immutable(statement: Statement) -> None:
    with pytest.raises(FrozenInstanceError):
        statement.bank_id = "changed"
    with pytest.raises(FrozenInstanceError):
        statement.transactions[0].amount = Decimal("1")


def test_balance_mismatch_fails(statement: Statement) -> None:
    with pytest.raises(StatementValidationError, match="closing balance"):
        validate_statement(replace(statement, closing_balance=Decimal("1365.01")))


@pytest.mark.parametrize("changes", [
    {"transactions": ()}, {"period_start": date(2026, 10, 1)},
    {"period_end": "05/09/2026"}, {"layout_id": None},
    {"opening_balance": Decimal("NaN")}, {"closing_balance": Decimal("Infinity")},
    {"opening_balance": 1000}, {"closing_balance": Decimal("1365.001")},
])
def test_incomplete_or_invalid_statements_fail(statement: Statement, changes: dict) -> None:
    with pytest.raises(StatementValidationError):
        validate_statement(replace(statement, **changes))


@pytest.mark.parametrize("changes", [
    {"posting_date": "01/09/2026"}, {"posting_date": datetime(2026, 9, 1)},
    {"posting_date": date(2026, 8, 31)}, {"posting_date": date(2026, 9, 6)},
    {"description": " "}, {"amount": 500.0}, {"amount": Decimal("NaN")},
    {"amount": Decimal("Infinity")}, {"amount": Decimal("500.001")},
])
def test_invalid_transactions_fail(statement: Statement, changes: dict) -> None:
    first = replace(statement.transactions[0], **changes)
    with pytest.raises(StatementValidationError):
        validate_statement(replace(statement, transactions=(first, *statement.transactions[1:])))


def test_transactions_are_not_silently_reordered(statement: Statement) -> None:
    with pytest.raises(StatementValidationError, match="order"):
        validate_statement(replace(statement, transactions=statement.transactions[::-1]))


def test_optional_balances_are_allowed_in_normalized_model(statement: Statement) -> None:
    validate_statement(replace(statement, opening_balance=None, closing_balance=None))


def test_decimal_context_cannot_hide_one_cent_mismatch(statement: Statement) -> None:
    with localcontext() as context:
        context.prec = 2
        validate_statement(statement)
        with pytest.raises(StatementValidationError, match="closing balance"):
            validate_statement(replace(statement, closing_balance=Decimal("1365.01")))


def test_large_values_reconcile_exactly(statement: Statement) -> None:
    transaction = Transaction(date(2026, 9, 1), "SYNTHETIC LARGE VALUE", Decimal("0.01"))
    large = replace(statement, opening_balance=Decimal("1000000000000000000000000000000.00"),
                    closing_balance=Decimal("1000000000000000000000000000000.01"),
                    transactions=(transaction,))
    validate_statement(large)
    with pytest.raises(StatementValidationError):
        validate_statement(replace(large, closing_balance=large.opening_balance))


def test_trailing_decimal_zeros_do_not_invalidate_cents(statement: Statement) -> None:
    validate_statement(replace(statement, closing_balance=Decimal("1365.0000")))


def test_absent_bank_identity_does_not_affect_financial_validity(statement: Statement) -> None:
    assert validate_statement(replace(statement, bank_id="", account=None)) == validate_statement(statement)
