"""Local financial declarations with precise failure capabilities.

The observed period, balance and summary label bindings from M4/M5 are combined
here. Missing controls remain missing; identity is never used for binding.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from pdf_to_ofx.domain.errors import StatementValidationError
from pdf_to_ofx.domain.evidence import FinancialRole, MonetaryAssignment
from pdf_to_ofx.generic.operators.amounts import MonetaryDomain, RoleDecision
from pdf_to_ofx.generic.operators.candidates import OperatorFailure, _period
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.semantics import balance_labels, has_financial_signal, money_regions
from pdf_to_ofx.generic.structure import Row
from pdf_to_ofx.validation.checkpoints import ConstraintViolation

SUMMARY = {
    "saldo inicial": ("opening_balance", FinancialRole.OPENING_BALANCE),
    "saldo anterior": ("opening_balance", FinancialRole.OPENING_BALANCE),
    "saldo final": ("closing_balance", FinancialRole.CLOSING_BALANCE),
    "saldo final do período": ("closing_balance", FinancialRole.CLOSING_BALANCE),
    "saldo total": ("closing_balance", FinancialRole.CLOSING_BALANCE),
    "total de entradas": ("credits", FinancialRole.CREDIT_TOTAL),
    "total de créditos": ("credits", FinancialRole.CREDIT_TOTAL),
    "total de saídas": ("debits", FinancialRole.DEBIT_TOTAL),
    "total de débitos": ("debits", FinancialRole.DEBIT_TOTAL),
    "rendimento líquido": ("yield", FinancialRole.SUMMARY_ADJUSTMENT),
}


def read_financial_context(header: tuple[Row, ...], profile: LayoutProfile,
                           supplied: StatementContext | None, coverage: VisualCoverage,
                           role_decisions: list[RoleDecision] | None = None,
                           ) -> tuple[StatementContext, dict[str, Decimal], tuple[MonetaryDomain, ...]]:
    context = supplied or StatementContext()
    declarations: dict[str, date | Decimal] = {}
    totals: dict[str, Decimal] = {}
    claimed: set[int] = set()
    domains = []

    def declare(field: str, value: date | Decimal) -> None:
        target = declarations if field in StatementContext.__dataclass_fields__ else totals
        previous = target.get(field, getattr(context, field, None))
        if previous is not None and previous != value:
            raise StatementValidationError("Conflicting statement period or financial declarations.")
        target[field] = value

    for index, row in enumerate(header):
        period = _period(row)
        if period:
            declare("period_start", period[0])
            declare("period_end", period[1])
            claimed.add(index)
            continue
        regions = money_regions(row, profile.tolerances)
        label = " ".join(w.text for w in row.words[:regions[0].start]) if regions else row.text
        # An unqualified balance is observed, but has no declared temporal anchor.
        # These are the only two boundary roles supported here; unknown captions
        # do not acquire guessed roles merely because their arithmetic might fit.
        if label.casefold().rstrip(":") == "saldo" and len(regions) == 1 and regions[0].end == len(row.words):
            region = regions[0]
            source = coverage.span(row, region.start, region.end)
            roles = (FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE)
            domains.append(MonetaryDomain(source, region, roles,
                tuple(RoleDecision(source, role, "balance_label", True, "unqualified_balance_label", (coverage.span(row),))
                      for role in roles)))
            claimed.add(index)
            continue
        named = SUMMARY.get(label.casefold().rstrip(":"))
        labels = balance_labels(label) if label.casefold().startswith("saldo ") else ()
        if named is None and not labels:
            continue
        value_index = index
        if not regions:
            candidates = []
            for other_index in range(index + 1, len(header)):
                other = header[other_index]
                if other.page != row.page or other.words[0].top - row.words[0].bottom > profile.tolerances.summary_y:
                    break
                values = money_regions(other, profile.tolerances)
                if (values and values[0].start == 0 and values[-1].end == len(other.words)
                        and all(a.end == b.start for a, b in zip(values, values[1:]))
                        and (len(labels) > 1 and other_index == index + 1
                             or abs(other.words[0].x0 - row.words[0].x0) <= profile.tolerances.row_y)):
                    candidates.append((other_index, values))
            if len(candidates) != 1:
                raise OperatorFailure("summary_value_binding", "A financial label requires one complete aligned value region.")
            value_index, regions = candidates[0]
        if named:
            bindings = (named,)
        else:
            bindings = tuple(
                ("opening_balance", FinancialRole.OPENING_BALANCE) if name in {"inicial", "anterior"}
                else ("closing_balance", FinancialRole.CLOSING_BALANCE) if name in {"final", "total"}
                else (None, FinancialRole.BALANCE_COMPONENT) for name in labels)
        if len(bindings) != len(regions):
            raise OperatorFailure("balance_label_binding", "Balance labels and monetary regions disagree.")
        consumed = {i for region in regions for i in range(region.start, region.end)}
        unconsumed = " ".join(w.text for i, w in enumerate(header[value_index].words) if i not in consumed)
        if has_financial_signal(unconsumed) or any(
                w.text in {"-", "+"} for i, w in enumerate(header[value_index].words) if i not in consumed):
            raise OperatorFailure("monetary_token_incomplete", "A financial declaration contains incomplete monetary tokens.")
        if regions[-1].end != len(header[value_index].words):
            raise OperatorFailure("balance_label_binding", "A financial declaration has incomplete value regions.")
        for (field, role), region in zip(bindings, regions):
            if field:
                declare(field, region.money.amount)
            coverage.monetary(header[value_index], region, role)
            if role_decisions is not None:
                source = coverage.span(header[value_index], region.start, region.end)
                label_end = regions[0].start if value_index == index else len(row.words)
                role_decisions.append(RoleDecision(source, role, "declaration_label", True, "exact_financial_label_binding",
                    (coverage.span(row, 0, label_end), source)))
        claimed.update((index, value_index))
    for i, row in enumerate(header):
        if has_financial_signal(row.text) and i not in claimed:
            regions = money_regions(row, profile.tolerances)
            consumed = {i for region in regions for i in range(region.start, region.end)}
            remaining = " ".join(w.text for i, w in enumerate(row.words) if i not in consumed)
            if not regions or has_financial_signal(remaining) or any(
                    w.text in {"+", "-"} for i, w in enumerate(row.words) if i not in consumed):
                raise OperatorFailure("monetary_token_incomplete", "Financial content has incomplete monetary tokens.")
            # Observation is complete. Ownership and relational generators run
            # after transaction segmentation, before a role can be assigned.
            domains.extend(MonetaryDomain(coverage.span(row, region.start, region.end), region, ()) for region in regions)
    context = replace(context, **declarations)
    if context.period_start is None or context.period_end is None:
        raise OperatorFailure("period_declaration_missing", "An explicit full-year statement period is required.")
    if totals.get("yield", Decimal("0.00")) != 0:
        raise OperatorFailure("summary_adjustment_undetailed", "A nonzero summary adjustment requires detailed movements.")
    return context, totals, tuple(domains)


def bind_context_roles(context: StatementContext, domains: tuple[MonetaryDomain, ...],
                       assignments: tuple[MonetaryAssignment, ...]) -> StatementContext:
    """Apply a partial assignment without guessing missing boundary values."""
    if len(assignments) > len(domains):
        raise ConstraintViolation("monetary_domain_membership")
    fields = {FinancialRole.OPENING_BALANCE: "opening_balance", FinancialRole.CLOSING_BALANCE: "closing_balance"}
    for domain, assignment in zip(domains, assignments):
        if (assignment.source != domain.source or assignment.role not in domain.roles
                or assignment.transaction_index is not None):
            raise ConstraintViolation("monetary_domain_membership")
        if assignment.role == FinancialRole.SPARSE_CHECKPOINT:
            if domain.boundary is None:
                raise ConstraintViolation("checkpoint_interval_missing")
            continue
        if assignment.role not in fields:
            raise ConstraintViolation("monetary_domain_membership")
        field, value = fields[assignment.role], domain.region.money.amount
        previous = getattr(context, field)
        if previous is not None and previous != value:
            raise ConstraintViolation("financial_declarations")
        context = replace(context, **{field: value})
    return context
