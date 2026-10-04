"""Strict grammar for the single fictitious M0 layout, in document order."""

import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.domain.evidence import EvidenceStatus, FinancialRole, Interpretation
from pdf_to_ofx.domain.models import Chronology, Statement, Transaction
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.pdf.provenance import text_lines, text_span
from pdf_to_ofx.validation.coverage import CoverageLedger
from pdf_to_ofx.validation.statement import validate_statement

HEADERS = ("BANCO SINTETICO", "EXTRATO CONTA CORRENTE")
DATE_PATTERN = r"\d{2}/\d{2}/\d{4}"
MONEY_PATTERN = r"(?:0|[1-9]\d{0,2}(?:\.\d{3})*|[1-9]\d*),\d{2} [CD]"
PERIOD = re.compile(rf"Periodo: ({DATE_PATTERN}) a ({DATE_PATTERN})", re.ASCII)
# M0 descriptions are uppercase ASCII words; extra amount columns are rejected.
TRANSACTION = re.compile(rf"({DATE_PATTERN})\s+([A-Z][A-Z0-9 ]*?)\s+({MONEY_PATTERN})", re.ASCII)


def parse_brazilian_money(value: str) -> Decimal:
    """Parse explicit Brazilian cents and C/D signs, independent of locale."""
    if not re.fullmatch(MONEY_PATTERN, value, flags=re.ASCII):
        raise StatementParseError("Invalid Brazilian monetary value.")
    number, direction = value.split(" ")
    amount = Decimal(number.replace(".", "").replace(",", "."))
    return amount.copy_negate() if direction == "D" else amount


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as exc:
        raise StatementParseError("Invalid calendar date in synthetic statement.") from exc


def document_lines(document: ExtractedDocument) -> tuple[str, ...]:
    # Blank lines and surrounding whitespace carry no meaning in this layout.
    return tuple(
        line.strip()
        for page in document.pages
        for line in page.text.splitlines()
        if line.strip()
    )


class SyntheticParser:
    bank_name = "Synthetic Bank"
    layout_id = "synthetic-v1"

    def parse(self, document: ExtractedDocument) -> Statement:
        lines = document_lines(document)
        if len(lines) < 5 or lines[:2] != HEADERS:
            raise StatementParseError("Missing synthetic statement headers or fields.")
        period = PERIOD.fullmatch(lines[2])
        if period is None:
            raise StatementParseError("Missing or malformed statement period.")
        opening = self._balance(lines[3], "Saldo anterior: ")
        closing = self._balance(lines[-1], "Saldo final: ")
        transactions = []
        for line_number, line in enumerate(lines[4:-1], start=5):
            match = TRANSACTION.fullmatch(line)
            if match is None:
                # Every line in the transaction block must be accounted for.
                raise StatementParseError(
                    f"Malformed or unexpected transaction line {line_number}."
                )
            transactions.append(
                Transaction(
                    posting_date=_parse_date(match[1]),
                    description=match[2],
                    amount=parse_brazilian_money(match[3]),
                )
            )
        return Statement(
            bank_id="synthetic",
            layout_id=self.layout_id,
            period_start=_parse_date(period[1]),
            period_end=_parse_date(period[2]),
            opening_balance=opening,
            closing_balance=closing,
            transactions=tuple(transactions),
            chronology=Chronology.ASCENDING,
        )

    def interpret(self, document: ExtractedDocument) -> Interpretation:
        statement = self.parse(document)
        lines = text_lines(document)
        expected = tuple(text_span(page, row, text, match.start(), match.end())
                         for page, row, text in lines for match in re.finditer(MONEY_PATTERN, text))
        coverage = CoverageLedger(expected)
        index = 0
        for page, row, text in lines:
            region = re.search(MONEY_PATTERN, text)
            if region is None:
                continue
            source = text_span(page, row, text, region.start(), region.end())
            if text.startswith("Saldo anterior: "):
                coverage.claim(source, FinancialRole.OPENING_BALANCE)
            elif text.startswith("Saldo final: "):
                coverage.claim(source, FinancialRole.CLOSING_BALANCE)
            else:
                coverage.claim(source, FinancialRole.MOVEMENT, index)
                coverage.transaction(text_span(page, row, text))
                match = TRANSACTION.fullmatch(text)
                assert match is not None
                coverage.field_sources(index, date=text_span(page, row, text, *match.span(1)),
                    description=(text_span(page, row, text, *match.span(2)),),
                    amount=source, direction=source, direction_basis="direction_marker", economic_order=index)
                index += 1
        evidence = replace(validate_statement(statement),
                           running_balance_verified=EvidenceStatus.NOT_APPLICABLE,
                           group_subtotals_verified=EvidenceStatus.NOT_APPLICABLE,
                           daily_balances_verified=EvidenceStatus.NOT_APPLICABLE)
        return coverage.finish(statement, evidence)

    @staticmethod
    def _balance(line: str, label: str) -> Decimal:
        if not line.startswith(label):
            raise StatementParseError("Missing or misplaced synthetic statement balance.")
        return parse_brazilian_money(line[len(label):])
