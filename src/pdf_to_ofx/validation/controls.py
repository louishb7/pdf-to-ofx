"""Independence of financial controls, without counting repetitions as proof."""

from dataclasses import dataclass

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.validation.checkpoints import ConstraintViolation


@dataclass(frozen=True, slots=True)
class ControlInterval:
    source: SourceSpan
    movements: tuple[SourceSpan, ...]
    role: FinancialRole
    # Half-open economic interval. None preserves the legacy set-only oracle.
    begin: int | None = None
    end: int | None = None
    references: tuple[SourceSpan, ...] = ()
    # Credit/debit totals may select nonadjacent movements within the scope.
    movement_indexes: tuple[int, ...] | None = None


def overlaps(left: SourceSpan, right: SourceSpan) -> bool:
    return ((left.page, left.row, left.basis) == (right.page, right.row, right.basis)
            and left.word_start < right.word_end and right.word_start < left.word_end)


def validate_control_intervals(controls: tuple[ControlInterval, ...], scope_id: str,
                               *, movement_order: tuple[SourceSpan | None, ...] | None = None,
                               scope_sources: tuple[SourceSpan, ...] | None = None) -> int:
    """Repeated declarations can confirm one interval, never multiply evidence.

    Explicit economic intervals support containment, including nested subtotals.
    Set-only callers cannot prove a hierarchy and keep the conservative oracle.
    Shared balance endpoints are legitimate references; duplicated owners and
    cyclic references are not. Equal coverage never multiplies independence.
    """
    all_movements = {s for control in controls for s in control.movements}
    if movement_order is not None:
        known = tuple(s for s in movement_order if s is not None)
        if any(overlaps(left, right) for i, left in enumerate(known) for right in known[:i]):
            raise ConstraintViolation("control_movement_repeated")
        all_movements.update(known)
    if any(s.region_id != scope_id for s in all_movements):
        raise ConstraintViolation("control_scope_or_membership")
    balance_roles = {FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE, FinancialRole.RUNNING_BALANCE,
                     FinancialRole.DAILY_BALANCE, FinancialRole.SPARSE_CHECKPOINT}
    intervals = set()
    owners = {c.source: c for c in controls}

    def in_scope(source: SourceSpan) -> bool:
        return source.region_id == scope_id and (scope_sources is None or any(
            (s.page, s.row, s.basis, s.region_id) == (source.page, source.row, source.basis, source.region_id)
            and s.word_start <= source.word_start < source.word_end <= s.word_end for s in scope_sources))

    for index, control in enumerate(controls):
        ordered = control.begin is not None or control.end is not None
        if (not isinstance(control.role, FinancialRole) or control.role == FinancialRole.MOVEMENT
                or not in_scope(control.source) or not ordered and not control.movements
                or any(not in_scope(s) for s in (*control.movements, *control.references))):
            raise ConstraintViolation("control_scope_or_membership")
        if any(overlaps(previous.source, control.source) for previous in controls[:index]):
            raise ConstraintViolation("control_source_reused")
        if ordered:
            if (type(control.begin) is not int or type(control.end) is not int or not 0 <= control.begin <= control.end
                    or movement_order is None or control.end > len(movement_order)
                    or control.begin == control.end and (control.role not in balance_roles or control.movements or control.references)):
                raise ConstraintViolation("control_interval_domain")
            if (control.role == FinancialRole.OPENING_BALANCE and (control.begin, control.end) != (0, 0)
                    or control.role == FinancialRole.CLOSING_BALANCE and control.end != len(movement_order)
                    or control.role == FinancialRole.RUNNING_BALANCE and control.end == 0
                    or control.role in {FinancialRole.CREDIT_TOTAL, FinancialRole.DEBIT_TOTAL}
                    and (control.begin, control.end) != (0, len(movement_order))):
                raise ConstraintViolation("control_interval_domain")
            indexes = control.movement_indexes
            if indexes is None:
                indexes = tuple(range(control.begin, control.end))
            elif (control.role not in {FinancialRole.CREDIT_TOTAL, FinancialRole.DEBIT_TOTAL}
                    or any(type(i) is not int or not control.begin <= i < control.end for i in indexes)
                    or tuple(sorted(set(indexes))) != indexes):
                raise ConstraintViolation("control_interval_membership")
            if control.movements != tuple(movement_order[i] for i in indexes if movement_order[i] is not None):
                raise ConstraintViolation("control_interval_membership")
        elif control.references or control.movement_indexes is not None:
            raise ConstraintViolation("control_interval_domain")
        if any(overlaps(left, right) for i, left in enumerate(control.movements) for right in control.movements[:i]):
            raise ConstraintViolation("control_movement_repeated")
        if any(overlaps(control.source, s) for s in all_movements):
            raise ConstraintViolation("circular_financial_control")
        if any(overlaps(s, m) for s in control.references for m in all_movements):
            raise ConstraintViolation("circular_financial_control")
        if any(overlaps(left, right) for i, left in enumerate(control.references) for right in control.references[:i]):
            raise ConstraintViolation("control_source_reused")
        for reference in control.references:
            if overlaps(reference, control.source):
                raise ConstraintViolation("circular_financial_control")
            owner = owners.get(reference)
            if owner is None:
                raise ConstraintViolation("control_reference_missing")
        members = frozenset(control.movements)
        for previous in controls[:index]:
            previous_members = frozenset(previous.movements)
            if previous.role == control.role == FinancialRole.SUBTOTAL:
                if ordered and previous.begin is not None and previous.end is not None:
                    crossing = max(control.begin, previous.begin) < min(control.end, previous.end) and not (
                        control.begin <= previous.begin <= previous.end <= control.end
                        or previous.begin <= control.begin <= control.end <= previous.end)
                else:
                    crossing = bool(members & previous_members and members != previous_members)
                if crossing:
                    raise ConstraintViolation("subtotal_interval_overlap")
        if control.movements:
            intervals.add(members)
    # Economic order rules out cycles for valid references. Check explicitly as
    # well, so malformed callers cannot hide a cycle behind equivalent objects.
    def visit(source: SourceSpan, active: set[SourceSpan], done: set[SourceSpan]) -> None:
        if source in active:
            raise ConstraintViolation("circular_financial_control")
        if source in done:
            return
        active.add(source)
        for reference in owners[source].references:
            visit(reference, active, done)
        active.remove(source)
        done.add(source)

    done: set[SourceSpan] = set()
    for source in owners:
        visit(source, set(), done)
    for control in controls:
        for reference in control.references:
            owner = owners[reference]
            if (control.role not in balance_roles or owner.role not in balance_roles
                    or owner.end != control.begin):
                raise ConstraintViolation("control_reference_order")
    return len(intervals)
