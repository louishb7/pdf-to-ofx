"""Local financial declarations with precise failure capabilities.

The observed period, balance and summary label bindings from M4/M5 are combined
here. Missing controls remain missing; identity is never used for binding.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from pdf_to_ofx.domain.errors import StatementValidationError
from pdf_to_ofx.domain.evidence import FinancialRole
from pdf_to_ofx.generic.operators.candidates import OperatorFailure, _period
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.semantics import balance_labels, has_financial_signal, money_regions
from pdf_to_ofx.generic.structure import Row

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
                           ) -> tuple[StatementContext, dict[str, Decimal]]:
    context = supplied or StatementContext()
    declarations: dict[str, date | Decimal] = {}
    totals: dict[str, Decimal] = {}
    claimed: set[int] = set()

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
            raise OperatorFailure("financial_region_unclassified", "Unclassified monetary content in a financial declaration.")
        if regions[-1].end != len(header[value_index].words):
            raise OperatorFailure("balance_label_binding", "A financial declaration has incomplete value regions.")
        for (field, role), region in zip(bindings, regions):
            if field:
                declare(field, region.money.amount)
            coverage.monetary(header[value_index], region, role)
        claimed.update((index, value_index))
    if any(has_financial_signal(row.text) and i not in claimed for i, row in enumerate(header)):
        raise OperatorFailure("financial_region_unclassified", "Unclassified financial content outside the transaction area.")
    context = replace(context, **declarations)
    if context.period_start is None or context.period_end is None:
        raise OperatorFailure("period_declaration_missing", "An explicit full-year statement period is required.")
    if totals.get("yield", Decimal("0.00")) != 0:
        raise OperatorFailure("summary_adjustment_undetailed", "A nonzero summary adjustment requires detailed movements.")
    return context, totals
