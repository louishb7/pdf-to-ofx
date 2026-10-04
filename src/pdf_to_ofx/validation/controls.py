"""Independence of financial controls, without counting repetitions as proof."""

from dataclasses import dataclass

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.validation.checkpoints import ConstraintViolation


@dataclass(frozen=True, slots=True)
class ControlInterval:
    source: SourceSpan
    movements: tuple[SourceSpan, ...]
    role: FinancialRole


def overlaps(left: SourceSpan, right: SourceSpan) -> bool:
    return ((left.page, left.row, left.basis) == (right.page, right.row, right.basis)
            and left.word_start < right.word_end and right.word_start < left.word_end)


def validate_control_intervals(controls: tuple[ControlInterval, ...], scope_id: str) -> int:
    """Repeated declarations can confirm one interval, never multiply evidence.

    Crossing/nested subtotal groups are not a supported hierarchy. Checkpoints
    and subtotals may constrain the same movements; this is corroboration, not a
    second independent interval. Their monetary occurrences still need ownership.
    """
    all_movements = {s for control in controls for s in control.movements}
    intervals = set()
    for index, control in enumerate(controls):
        if (not isinstance(control.role, FinancialRole) or control.role == FinancialRole.MOVEMENT
                or control.source.region_id != scope_id or not control.movements
                or any(s.region_id != scope_id for s in control.movements)):
            raise ConstraintViolation("control_scope_or_membership")
        if any(overlaps(left, right) for i, left in enumerate(control.movements) for right in control.movements[:i]):
            raise ConstraintViolation("control_movement_repeated")
        if any(overlaps(control.source, s) for s in all_movements):
            raise ConstraintViolation("circular_financial_control")
        members = frozenset(control.movements)
        for previous in controls[:index]:
            if overlaps(previous.source, control.source):
                raise ConstraintViolation("control_source_reused")
            previous_members = frozenset(previous.movements)
            if (previous.role == control.role == FinancialRole.SUBTOTAL and
                    members & previous_members and members != previous_members):
                raise ConstraintViolation("subtotal_interval_overlap")
        intervals.add(members)
    return len(intervals)
