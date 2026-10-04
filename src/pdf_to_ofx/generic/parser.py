"""Parse structural roles and reconcile exact values; no institution registry."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import groupby
import re

from pdf_to_ofx.domain.errors import (
    RecognizedInvalidStatementError, StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import EvidenceStatus, FinancialRole, Interpretation, SourceSpan
from pdf_to_ofx.domain.models import BankAccount, Chronology, Statement, Transaction
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import (
    LONG_DATE, NUMERIC_DATE, SHORT_MONTH_DATE, balance_labels, has_financial_signal, money_regions, parse_date,
)
from pdf_to_ofx.generic.structure import Row, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.validation.statement import validate_statement

PERIOD = re.compile(r"per[ií]odo:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})\s+(?:a|até)\s+([0-9]{2}/[0-9]{2}/[0-9]{4})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StatementContext:
    """Document data supplied separately from the portable structural profile.

    Unknown identity is explicit, never a synthetic OFX account. Calendar
    periods and declared balances are read from generic financial labels unless
    supplied independently; conflicting declarations always fail.
    """
    bank_id: str = "unknown"
    account: BankAccount | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.bank_id, str)
                or (self.account is not None and not isinstance(self.account, BankAccount))):
            raise StatementValidationError("Invalid explicit statement identity context.")
        if any(value is not None and type(value) is not date for value in (self.period_start, self.period_end)):
            raise StatementValidationError("Explicit context periods require calendar dates.")
        if any(value is not None and (not isinstance(value, Decimal) or not value.is_finite())
               for value in (self.opening_balance, self.closing_balance)):
            raise StatementValidationError("Explicit context balances require finite Decimal values.")


def leading_date(row: Row) -> tuple[date, int] | None:
    for count, pattern in ((1, NUMERIC_DATE), (5, LONG_DATE), (3, SHORT_MONTH_DATE)):
        text = " ".join(word.text for word in row.words[:count])
        if pattern.fullmatch(text):
            value = parse_date(text)
            if value is None:
                raise StatementParseError("Invalid calendar date in structural row.")
            return value, count
    return None


def strip_footers(rows: tuple[Row, ...], profile: LayoutProfile) -> tuple[Row, ...]:
    output = []
    for _, grouped in groupby(rows, key=lambda row: row.page):
        page = list(grouped)
        if profile.footer_rows:
            if len(page) <= profile.footer_rows:
                raise StatementParseError("Footer consumes a complete page.")
            for row in page[-profile.footer_rows:]:
                if (has_financial_signal(row.text) or NUMERIC_DATE.search(row.text)
                        or LONG_DATE.search(row.text)):
                    raise StatementParseError("Financial or dated content cannot be ignored as a footer.")
            page = page[:-profile.footer_rows]
        output.extend(page)
    return tuple(output)


def read_context(header: tuple[Row, ...], profile: LayoutProfile,
                 supplied: StatementContext | None, coverage: VisualCoverage) -> StatementContext:
    context = supplied or StatementContext()
    declarations: dict[str, date | Decimal] = {}
    claimed: set[int] = set()

    def declare(field: str, value: date | Decimal) -> None:
        existing = declarations.get(field, getattr(context, field))
        if existing is not None and existing != value:
            raise StatementValidationError("Conflicting statement period or balance declarations.")
        declarations[field] = value

    for index, row in enumerate(header):
        period = PERIOD.fullmatch(row.text)
        if period:
            start, end = parse_date(period[1]), parse_date(period[2])
            if start is None or end is None:
                raise StatementParseError("Invalid declared statement period.")
            declare("period_start", start)
            declare("period_end", end)
            claimed.add(index)
        labels = balance_labels(row.text) if row.text.casefold().startswith("saldo ") else ()
        if not labels:
            continue
        regions = money_regions(row, profile.tolerances)
        value_row = index
        if not regions and index + 1 < len(header):
            value_row = index + 1
            regions = money_regions(header[value_row], profile.tolerances)
            if (not regions or regions[0].start != 0 or regions[-1].end != len(header[value_row].words)
                    or any(a.end != b.start for a, b in zip(regions, regions[1:]))):
                raise StatementParseError("Declared balances require a complete numeric row.")
        if len(labels) != len(regions):
            raise StatementParseError("Balance labels and monetary regions disagree.")
        consumed = {i for region in regions for i in range(region.start, region.end)}
        unconsumed = " ".join(word.text for i, word in enumerate(header[value_row].words) if i not in consumed)
        if (has_financial_signal(unconsumed)
                or any(word.text in {"-", "+"} for i, word in enumerate(header[value_row].words) if i not in consumed)):
            raise StatementParseError("Unclassified monetary content in declared balances.")
        claimed.update((index, value_row))
        for label, region in zip(labels, regions):
            if label in {"inicial", "anterior"}:
                declare("opening_balance", region.money.amount)
                role = FinancialRole.OPENING_BALANCE
            elif label in {"final", "total"}:
                declare("closing_balance", region.money.amount)
                role = FinancialRole.CLOSING_BALANCE
            else:
                role = FinancialRole.BALANCE_COMPONENT
            coverage.monetary(header[value_row], region, role)
    for index, row in enumerate(header):
        if has_financial_signal(row.text) and index not in claimed:
            raise StatementParseError("Unclassified financial content precedes the transaction area.")
    context = replace(context, **declarations)
    if type(context.period_start) is not date or type(context.period_end) is not date:
        raise StatementParseError("An explicit statement period is required.")
    return context


class GenericStatementParser:
    layout_id = "generic-structural-v1"

    def parse(self, document: ExtractedDocument, profile: LayoutProfile,
              *, context: StatementContext | None = None) -> Statement:
        return self.interpret(document, profile, context=context).statement

    def interpret(self, document: ExtractedDocument, profile: LayoutProfile,
                  *, context: StatementContext | None = None) -> Interpretation:
        if profile.amount_mode == AmountMode.GROUP_SUBTOTAL:
            # Import locally to keep the existing row grammar independent of
            # the additional structural family, without a bank parser registry.
            from pdf_to_ofx.generic.grouped import interpret_grouped_subtotals
            return interpret_grouped_subtotals(document, profile, context=context)
        original = reconstruct_rows(document, profile.tolerances)
        coverage = VisualCoverage(original, profile.tolerances)
        rows = strip_footers(original, profile)
        start = next((index for index, row in enumerate(rows) if leading_date(row) is not None), None)
        if start is None:
            raise StatementParseError("No dated transaction area was found.")
        context = read_context(rows[:start], profile, context, coverage)
        transactions: list[Transaction] = []
        current_date: date | None = None
        daily_balance: Decimal | None = None
        group_start = 0
        previous_page = rows[start].page
        date_source: SourceSpan | None = None
        daily_checks: list[bool] = []

        def check_group() -> None:
            if profile.date_mode == DateMode.GROUPED and current_date is not None:
                if len(transactions) == group_start:
                    raise StatementParseError("A date group contains no transactions.")
                if daily_balance is not None:
                    daily_checks.append(transactions[-1].balance_after == daily_balance)

        for row in rows[start:]:
            if row.page != previous_page and not profile.carry_date_across_pages:
                check_group()
                current_date = None
                date_source = None
            previous_page = row.page
            dated = leading_date(row)
            regions = money_regions(row, profile.tolerances)
            if profile.date_mode == DateMode.GROUPED and dated:
                check_group()
                current_date, date_end = dated
                date_source = coverage.span(row, 0, date_end)
                group_start = len(transactions)
                remainder = " ".join(word.text for word in row.words[date_end:])
                if not remainder:
                    daily_balance = None
                else:
                    if (balance_labels(remainder) not in (("do dia",), ("diário",))
                            or len(regions) != 1 or profile.balance_mode != BalanceMode.RUNNING):
                        raise StatementParseError("Unsupported or malformed date-group heading.")
                    before = " ".join(w.text for w in row.words[date_end:regions[0].start])
                    if not re.fullmatch(r"saldo\s+(?:do dia|diário):?", before, re.IGNORECASE):
                        raise StatementParseError("Unclassified text in date-group heading.")
                    # Optional column captions are financial roles, not bank names.
                    captions = {"valor", "saldo", "por", "transação", "movimento", "movimentação"}
                    if any(w.text.strip(":").casefold() not in captions for w in row.words[regions[0].end:]):
                        raise StatementParseError("Unclassified trailing content in date-group heading.")
                    daily_balance = regions[0].money.amount
                    coverage.monetary(row, regions[0], FinancialRole.DAILY_BALANCE)
                continue
            date_end = 0
            if profile.date_mode == DateMode.PER_TRANSACTION:
                if dated is None:
                    raise StatementParseError("Every transaction requires its own date.")
                current_date, date_end = dated
                date_source = coverage.span(row, 0, date_end)
            if current_date is None:
                raise StatementParseError("A transaction has no date context.")
            expected = 2 if profile.balance_mode == BalanceMode.RUNNING else 1
            if (len(regions) != expected or regions[0].start <= date_end
                    or regions[-1].end != len(row.words)
                    or any(a.end != b.start for a, b in zip(regions, regions[1:]))):
                raise StatementParseError("Transaction monetary regions or description are incomplete.")
            description = " ".join(word.text for word in row.words[date_end:regions[0].start])
            if has_financial_signal(description) or row.words[regions[0].start - 1].text in {"-", "+"}:
                raise StatementParseError("Unclassified monetary content in transaction description.")
            if description.casefold().startswith("saldo ") and balance_labels(description):
                raise StatementParseError("A balance heading cannot be interpreted as a transaction.")
            movement = regions[profile.movement_column].money
            if ((profile.amount_mode == AmountMode.SIGNED and movement.marker is not None)
                    or (profile.amount_mode == AmountMode.CREDIT_DEBIT_MARKER and movement.marker is None)):
                raise StatementParseError("Transaction direction does not match the structural profile.")
            balance = regions[profile.balance_column].money.amount if profile.balance_column is not None else None
            index = len(transactions)
            coverage.monetary(row, regions[profile.movement_column], FinancialRole.MOVEMENT, index)
            if profile.balance_column is not None:
                coverage.monetary(row, regions[profile.balance_column], FinancialRole.RUNNING_BALANCE, index)
            assert date_source is not None
            coverage.transaction(date_source, coverage.span(row))
            transactions.append(Transaction(current_date, description, movement.amount, balance))
        check_group()
        statement = Statement(
            bank_id=context.bank_id, layout_id=self.layout_id,
            period_start=context.period_start, period_end=context.period_end,
            opening_balance=context.opening_balance, closing_balance=context.closing_balance,
            transactions=tuple(transactions), account=context.account,
            chronology=Chronology.ASCENDING,
            running_balances_required=profile.balance_mode == BalanceMode.RUNNING,
        )
        try:
            evidence = validate_statement(statement)
            if not all(daily_checks):
                raise StatementValidationError("Daily closing balance differs from its last transaction.")
        except StatementValidationError as error:
            raise RecognizedInvalidStatementError(str(error)) from error
        evidence = replace(evidence,
            running_balance_verified=evidence.running_balance_verified if profile.balance_mode == BalanceMode.RUNNING
                else EvidenceStatus.NOT_APPLICABLE,
            group_subtotals_verified=EvidenceStatus.NOT_APPLICABLE,
            daily_balances_verified=EvidenceStatus.VERIFIED if daily_checks else EvidenceStatus.NOT_AVAILABLE,
        )
        return coverage.finish(statement, evidence)
