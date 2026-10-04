"""Immutable values; positive amounts are credits, negative amounts debits."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Transaction:
    posting_date: date
    description: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Statement:
    bank_id: str
    layout_id: str
    period_start: date
    period_end: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions: tuple[Transaction, ...]
