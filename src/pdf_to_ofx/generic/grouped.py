"""Unsigned, wrapped transactions bounded by explicit signed flow subtotals.

Direction comes from the document's declared groups, never a bank identity,
transaction keyword, external OFX or a guessed amount sign.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from pdf_to_ofx.domain.errors import (
    RecognizedInvalidStatementError, StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import EvidenceStatus, FinancialRole, Interpretation, SourceSpan
from pdf_to_ofx.domain.models import Chronology, Statement, Transaction
from pdf_to_ofx.generic.operators.candidates import _has_date, _stamp, _period, _flow
from pdf_to_ofx.generic.operators.financial import _sum
from pdf_to_ofx.generic.operators.pages import continue_pages as _frame
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.parser import StatementContext, leading_date
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import (
    has_financial_signal, money_regions,
)
from pdf_to_ofx.generic.structure import Row, Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.validation.statement import validate_statement

SUMMARY = {
    "saldo inicial": "opening_balance", "saldo anterior": "opening_balance",
    "saldo final": "closing_balance", "saldo final do período": "closing_balance",
    "total de entradas": "credits", "total de créditos": "credits",
    "total de saídas": "debits", "total de débitos": "debits",
    "rendimento líquido": "yield",
}


def _context(header: tuple[Row, ...], profile: LayoutProfile,
             supplied: StatementContext | None, coverage: VisualCoverage) -> tuple[StatementContext, dict[str, Decimal]]:
    context = supplied or StatementContext()
    declarations: dict[str, date | Decimal] = {}
    totals: dict[str, Decimal] = {}
    claimed: set[int] = set()

    def declare(field: str, value: date | Decimal) -> None:
        target = declarations if field in StatementContext.__dataclass_fields__ else totals
        existing = target.get(field, getattr(context, field, None))
        if existing is not None and existing != value:
            raise StatementValidationError("Conflicting period or financial summary declarations.")
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
        role = SUMMARY.get(label.casefold().rstrip(":"))
        if role is None:
            continue
        value_index = index
        if not regions:
            # Bind a wrapped label to a value below it in the same visual region,
            # rather than borrowing a value from the adjacent summary column.
            candidates = []
            for other_index in range(index + 1, len(header)):
                other = header[other_index]
                if other.words[0].top - row.words[0].bottom > profile.tolerances.summary_y:
                    break
                other_regions = money_regions(other, profile.tolerances)
                if (len(other_regions) == 1 and other_regions[0].start == 0
                        and other_regions[0].end == len(other.words)
                        and abs(other.words[0].x0 - row.words[0].x0) <= profile.tolerances.row_y):
                    candidates.append((other_index, other_regions))
            if len(candidates) != 1:
                raise StatementParseError("A wrapped summary label requires one aligned monetary value.")
            value_index, regions = candidates[0]
        if (len(regions) != 1 or regions[0].end != len(header[value_index].words)
                or regions[0].money.marker is not None):
            raise StatementParseError("A summary requires one complete monetary value.")
        declare(role, regions[0].money.amount)
        financial_role = {
            "opening_balance": FinancialRole.OPENING_BALANCE,
            "closing_balance": FinancialRole.CLOSING_BALANCE,
            "credits": FinancialRole.CREDIT_TOTAL,
            "debits": FinancialRole.DEBIT_TOTAL,
            "yield": FinancialRole.SUMMARY_ADJUSTMENT,
        }[role]
        coverage.monetary(header[value_index], regions[0], financial_role)
        claimed.update((index, value_index))
    if any(has_financial_signal(row.text) and i not in claimed for i, row in enumerate(header)):
        raise StatementParseError("Unclassified financial content in the statement summary.")
    context = replace(context, **declarations)
    if (context.period_start is None or context.period_end is None
            or context.opening_balance is None or context.closing_balance is None):
        raise StatementParseError("Grouped flows require explicit period and opening/closing balances.")
    if totals.get("yield", Decimal("0.00")) != 0:
        raise StatementParseError("A nonzero summary adjustment requires detailed transactions.")
    return context, totals


def parse_grouped_subtotals(document: ExtractedDocument, profile: LayoutProfile,
                           *, context: StatementContext | None = None) -> Statement:
    return interpret_grouped_subtotals(document, profile, context=context).statement


def interpret_grouped_subtotals(document: ExtractedDocument, profile: LayoutProfile,
                                *, context: StatementContext | None = None) -> Interpretation:
    original = reconstruct_rows(document, profile.tolerances)
    coverage = VisualCoverage(original, profile.tolerances)
    rows = _frame(original, profile)
    start = next((i for i, row in enumerate(rows) if not _period(row) and leading_date(row)), None)
    if start is None:
        raise StatementParseError("No dated flow group was found.")
    context, totals = _context(rows[:start], profile, context, coverage)
    transactions: list[Transaction] = []
    current_date: date | None = None
    expected: Decimal | None = None
    direction: int | None = None
    group_start = 0
    previous_page = rows[start].page
    date_source: SourceSpan | None = None
    flow_source: SourceSpan | None = None
    subtotal_checks: list[bool] = []
    numeric_regions = [r for row in rows[start:] for r in money_regions(row, profile.tolerances)]
    if not numeric_regions:
        raise StatementParseError("Dated flows contain no monetary regions.")
    value_left = min(r.x0 for r in numeric_regions)

    def check_group() -> None:
        if expected is not None:
            amounts = [t.amount for t in transactions[group_start:]]
            subtotal_checks.append(bool(amounts) and _sum(amounts) == expected)

    for row in rows[start:]:
        if row.page != previous_page and not profile.carry_date_across_pages:
            check_group()
            current_date = direction = expected = None
            date_source = flow_source = None
        previous_page = row.page
        dated = leading_date(row)
        flow = _flow(row, profile.tolerances)
        if dated:
            check_group()
            current_date = dated[0]
            date_source = coverage.span(row, 0, dated[1])
            expected = direction = None
            if flow is None and dated[1] != len(row.words):
                raise StatementParseError("Date headings require a complete declared flow subtotal.")
        if flow is not None:
            if not dated:
                check_group()
            if current_date is None:
                raise StatementParseError("A flow subtotal has no date context.")
            expected, direction = flow
            region = money_regions(row, profile.tolerances)[0]
            coverage.monetary(row, region, FinancialRole.SUBTOTAL)
            flow_source = coverage.span(row)
            group_start = len(transactions)
            continue
        if dated:
            continue
        if current_date is None or expected is None:
            raise StatementParseError("A transaction has no dated direction group.")
        regions = money_regions(row, profile.tolerances)
        if regions:
            region = regions[0]
            if (len(regions) != 1 or region.start == 0 or region.end != len(row.words)
                    or region.money.explicit_sign or region.money.marker is not None
                    or abs(row.words[0].x0 - profile.transaction_left) > profile.tolerances.row_y):
                raise StatementParseError("Unsigned transaction does not match its declared description/value regions.")
            description = " ".join(w.text for w in row.words[:region.start])
            if has_financial_signal(description):
                raise StatementParseError("Unclassified monetary content in the transaction description.")
            amount = region.money.amount if direction == 1 else region.money.amount.copy_negate()
            coverage.monetary(row, region, FinancialRole.MOVEMENT, len(transactions))
            assert date_source is not None and flow_source is not None
            coverage.transaction(date_source, flow_source, coverage.span(row))
            coverage.field_sources(len(transactions), date=date_source,
                description=(coverage.span(row, 0, region.start),),
                amount=coverage.span(row, region.start, region.end), direction=flow_source,
                direction_basis="signed_group_subtotal", economic_order=len(transactions))
            transactions.append(Transaction(current_date, description, amount))
        else:
            if (not transactions or len(transactions) == group_start or has_financial_signal(row.text)
                    or _has_date(row.text) or profile.continuation_left is None
                    or abs(row.words[0].x0 - profile.continuation_left) > profile.tolerances.row_y
                    or row.words[-1].x1 >= value_left):
                raise StatementParseError("Unclassified or orphan transaction continuation.")
            transactions[-1] = replace(transactions[-1], description=transactions[-1].description + " " + row.text)
            coverage.continuation(coverage.span(row))
    check_group()
    credits = _sum([t.amount for t in transactions if t.amount >= 0])
    debits = _sum([t.amount for t in transactions if t.amount < 0])
    statement = Statement(context.bank_id, "generic-structural-v1", context.period_start,
                          context.period_end, context.opening_balance, context.closing_balance,
                          tuple(transactions), context.account, chronology=Chronology.ASCENDING,
                          currency=context.currency)
    try:
        if not all(subtotal_checks):
            raise StatementValidationError("Transactions do not reconcile with their declared flow subtotal.")
        if (totals.get("credits", credits) != credits or totals.get("debits", debits) != debits):
            raise StatementValidationError("Transactions differ from declared statement credit/debit totals.")
        evidence = validate_statement(statement)
    except StatementValidationError as error:
        raise RecognizedInvalidStatementError(str(error)) from error
    evidence = replace(evidence,
        running_balance_verified=EvidenceStatus.NOT_APPLICABLE,
        group_subtotals_verified=EvidenceStatus.VERIFIED,
        credit_total_verified=EvidenceStatus.VERIFIED if "credits" in totals else EvidenceStatus.NOT_AVAILABLE,
        debit_total_verified=EvidenceStatus.VERIFIED if "debits" in totals else EvidenceStatus.NOT_AVAILABLE,
    )
    return coverage.finish(statement, evidence)


def infer_grouped_profile(rows: tuple[Row, ...], tolerances: Tolerances) -> LayoutProfile | None:
    """Compatibility entry point for the extracted structural observation."""
    from pdf_to_ofx.generic.operators.geometry import observe_grouped_geometry
    return observe_grouped_geometry(rows, tolerances)
