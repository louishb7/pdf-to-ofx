"""Immutable values; positive amounts are credits, negative amounts debits."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Chronology(StrEnum):
    UNDECLARED = "undeclared"
    ASCENDING = "ascending"


@dataclass(frozen=True, slots=True)
class Transaction:
    posting_date: date
    description: str
    amount: Decimal
    balance_after: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BankAccount:
    organization: str
    institution_id: str
    bank_id: str
    branch_id: str
    account_id: str
    account_type: str = "CHECKING"


@dataclass(frozen=True, slots=True)
class Statement:
    bank_id: str
    layout_id: str
    period_start: date
    period_end: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions: tuple[Transaction, ...]
    account: BankAccount | None = None
    # Transactions retain document order. Only a parser's explicit declaration
    # allows that sequence to be used as economic order for balance links.
    chronology: Chronology = Chronology.UNDECLARED
    running_balances_required: bool = False
