"""Exact constraints between economic boundaries, also for partial assignments."""

from decimal import Context, Decimal, localcontext

from pdf_to_ofx.domain.errors import RecognizedInvalidStatementError
from pdf_to_ofx.domain.models import BalanceCheckpoint


def _sum(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0.00")
    precision = max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values)
    with localcontext(Context(prec=max(28, precision + len(str(len(values))) + 2))):
        return sum(values, Decimal("0.00"))


class ConstraintViolation(RecognizedInvalidStatementError):
    def __init__(self, constraint: str) -> None:
        self.constraint = constraint
        super().__init__(f"Structural hypothesis violates {constraint}.")


def check_checkpoints(amounts: tuple[Decimal | None, ...], checkpoints: tuple[BalanceCheckpoint, ...]) -> int:
    """Check a link as soon as all movements in its interval are known."""
    if any(value is not None and (not isinstance(value, Decimal) or not value.is_finite()) for value in amounts):
        raise ConstraintViolation("checkpoint_domain")
    boundaries: dict[int, Decimal] = {}
    for point in checkpoints:
        if (type(point.after) is not int or not 0 <= point.after <= len(amounts)
                or not isinstance(point.balance, Decimal) or not point.balance.is_finite()):
            raise ConstraintViolation("checkpoint_domain")
        previous = boundaries.get(point.after)
        if previous is not None and previous != point.balance:
            raise ConstraintViolation("checkpoint_same_boundary")
        boundaries[point.after] = point.balance
    ordered = sorted(boundaries.items())
    links = 0
    for (begin, earlier), (end, later) in zip(ordered, ordered[1:]):
        interval = amounts[begin:end]
        if any(value is None for value in interval):
            continue
        if _sum([earlier, *(value for value in interval if value is not None), later.copy_negate()]) != 0:
            raise ConstraintViolation("checkpoint_reconciliation")
        links += 1
    return links
