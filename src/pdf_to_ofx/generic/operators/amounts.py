"""Monetary roles and magnitude; no date or direction attribution."""

from dataclasses import dataclass
from decimal import Decimal
import re

from pdf_to_ofx.domain.evidence import FinancialRole
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.transactions import RowCandidate, TransactionSegment
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, LayoutProfile
from pdf_to_ofx.generic.semantics import MoneyRegion


@dataclass(frozen=True, slots=True)
class AmountRoles:
    movement: MoneyRegion
    magnitude: Decimal
    running_balance: MoneyRegion | None


@dataclass(frozen=True, slots=True)
class ControlRole:
    region: MoneyRegion
    role: FinancialRole


def infer_control_roles(candidate: RowCandidate, balance_mode: BalanceMode) -> tuple[ControlRole, ...]:
    """Only declared financial control regions receive control ownership."""
    if candidate.kind == "flow":
        return (ControlRole(candidate.amounts[0], FinancialRole.SUBTOTAL),)
    if candidate.kind != "date_heading":
        return ()
    row = candidate.row
    date_end = candidate.dates[0].end
    if date_end == len(row.words):
        return ()
    if len(candidate.amounts) != 1 or balance_mode != BalanceMode.RUNNING:
        raise OperatorFailure("amount_role_inference", "Unsupported monetary date-group control.")
    region = candidate.amounts[0]
    before = " ".join(w.text for w in row.words[date_end:region.start])
    captions = {"valor", "saldo", "por", "transação", "movimento", "movimentação"}
    if (not re.fullmatch(r"saldo\s+(?:do dia|diário):?", before, re.IGNORECASE)
            or any(w.text.strip(":").casefold() not in captions for w in row.words[region.end:])):
        raise OperatorFailure("amount_role_inference", "Unclassified date-group monetary content.")
    return (ControlRole(region, FinancialRole.DAILY_BALANCE),)


def infer_amount_roles(segment: TransactionSegment, profile: LayoutProfile) -> AmountRoles:
    head = segment.rows[0]
    regions = head.amounts
    expected = 2 if profile.balance_mode == BalanceMode.RUNNING else 1
    if (len(regions) != expected or regions[0].start == 0
            or regions[-1].end != len(head.row.words)
            or any(a.end != b.start for a, b in zip(regions, regions[1:]))):
        raise OperatorFailure("amount_role_inference", "Transaction monetary columns are incomplete.")
    if (profile.transaction_left is not None and
            abs(head.row.words[0].x0 - profile.transaction_left) > profile.tolerances.row_y):
        raise OperatorFailure("amount_role_inference", "Transaction description/value regions exceed the declared alignment tolerance.")
    movement = regions[profile.movement_column]
    money = movement.money
    if (profile.amount_mode != AmountMode.GROUP_SUBTOTAL
            and head.row.words[regions[0].start - 1].text in {"-", "+"}):
        raise OperatorFailure("amount_role_inference", "An unattached monetary sign cannot become description text.")
    if ((profile.amount_mode == AmountMode.GROUP_SUBTOTAL and (money.explicit_sign or money.marker is not None))
            or (profile.amount_mode == AmountMode.SIGNED and money.marker is not None)
            or (profile.amount_mode == AmountMode.CREDIT_DEBIT_MARKER and money.marker is None)):
        raise OperatorFailure("amount_role_inference", "Movement representation differs from the structural profile.")
    balance = regions[profile.balance_column] if profile.balance_column is not None else None
    return AmountRoles(movement, money.amount.copy_abs(), balance)
