"""Monetary roles and magnitude; no date or direction attribution."""

from dataclasses import dataclass
from decimal import Decimal
import re

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
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


@dataclass(frozen=True, slots=True)
class MonetaryDomain:
    """A discovered occurrence with admissible roles, not an ownership claim."""
    source: SourceSpan
    region: MoneyRegion
    roles: tuple[FinancialRole, ...]

    def __post_init__(self) -> None:
        if len(set(self.roles)) != len(self.roles) or any(not isinstance(r, FinancialRole) for r in self.roles):
            raise ValueError("Monetary domains require distinct financial roles.")


def infer_control_roles(candidate: RowCandidate, balance_mode: BalanceMode,
                        *, allow_checkpoints: bool = False) -> tuple[ControlRole, ...]:
    """Only declared financial control regions receive control ownership."""
    if candidate.kind == "flow":
        return (ControlRole(candidate.amounts[0], FinancialRole.SUBTOTAL),)
    if candidate.kind == "checkpoint" and allow_checkpoints:
        return (ControlRole(candidate.amounts[0], FinancialRole.SPARSE_CHECKPOINT),)
    if candidate.kind != "date_heading":
        return ()
    row = candidate.row
    date_end = candidate.dates[0].end
    if date_end == len(row.words):
        return ()
    if len(candidate.amounts) != 1 or not allow_checkpoints and balance_mode != BalanceMode.RUNNING:
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


def amount_role_candidates(segment: TransactionSegment, geometry: LayoutProfile,
                           *, constraint: LayoutProfile | None = None) -> tuple[AmountRoles, ...]:
    """One or two local role bindings, without interpreting the full document."""
    from dataclasses import replace
    size = len(segment.rows[0].amounts)
    if size not in (1, 2) or geometry.amount_mode == AmountMode.GROUP_SUBTOTAL and size != 1:
        return ()
    if constraint and size != (2 if constraint.balance_mode == BalanceMode.RUNNING else 1):
        return ()
    columns = (constraint.movement_column,) if constraint else range(size)
    output = []
    for column in columns:
        profile = replace(geometry, balance_mode=BalanceMode.RUNNING if size == 2 else BalanceMode.ABSENT,
                          movement_column=column, balance_column=1 - column if size == 2 else None)
        try:
            output.append(infer_amount_roles(segment, profile))
        except OperatorFailure:
            continue
    return tuple(output)
