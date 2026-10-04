"""Immutable values; positive amounts are credits, negative amounts debits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdf_to_ofx.domain.evidence import SourceSpan


class Chronology(StrEnum):
    UNDECLARED = "undeclared"
    UNKNOWN = "undeclared"  # Preserve the historical public spelling/value.
    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True)
class BalanceCheckpoint:
    """Balance after ``after`` movements in economic order, not document order."""
    after: int
    balance: Decimal
    kind: str
    source: SourceSpan | None = None


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
    checkpoints: tuple[BalanceCheckpoint, ...] = ()
