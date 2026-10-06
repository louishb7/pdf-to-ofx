"""Strict text grammar for the investigated Banco Inter digital layout.

Dates belong to groups, not individual rows. Group context crosses page breaks.
Only known header/footer roles are ignored; every body line is accounted for.
"""

import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from pdf_to_ofx.domain.currency import Currency
from pdf_to_ofx.domain.errors import StatementParseError, StatementValidationError
from pdf_to_ofx.domain.evidence import EvidenceStatus, FinancialRole, Interpretation
from pdf_to_ofx.domain.models import BankAccount, Chronology, Statement, Transaction
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.pdf.provenance import text_lines, text_span
from pdf_to_ofx.validation.coverage import CoverageLedger
from pdf_to_ofx.validation.statement import validate_statement

DATE = r"[0-9]{2}/[0-9]{2}/[0-9]{4}"
NUMBER = r"(?:0|[1-9][0-9]{0,2}(?:\.[0-9]{3})*|[1-9][0-9]*),[0-9]{2}"
MONEY = rf"-?R\$\s*{NUMBER}"
PERIOD = re.compile(rf"Período:\s*({DATE})\s+(?:a|até)\s+({DATE})")
EMISSION = re.compile(rf"Solicitado em: {DATE} - [0-9]{{1,2}}h[0-9]{{2}}")
ACCOUNT = re.compile(
    r".*?\bAgência:\s*([0-9]+(?:-[0-9]+)?),?\s+Conta:\s*([0-9]+(?:-[0-9]+)?)"
)
SUMMARY_HEADER = "Saldo total Saldo disponível: Saldo bloqueado:"
SUMMARY = re.compile(rf"({MONEY})\s+({MONEY})\s+({MONEY})")
INSTITUTION = re.compile(r"\bBanco Inter\b")
MONTHS = {
    name: index for index, name in enumerate(
        ("janeiro", "fevereiro", "março", "abril", "maio", "junho",
         "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"), start=1
    )
}
DAY = re.compile(
    rf"([0-9]{{1,2}}) de ([a-zç]+) de ([0-9]{{4}}) Saldo do dia:\s*({MONEY})"
    r"(?: Valor Saldo por transação)?", re.IGNORECASE
)
MOVEMENT = re.compile(rf"(\S(?:.*?\S)?)\s+({MONEY})\s+({MONEY})")


def matches_layout(document: ExtractedDocument) -> bool:
    lines = [line.strip() for page in document.pages for line in page.text.splitlines() if line.strip()]
    # Bank name belongs to the institution/account header, not a description.
    return (len(lines) >= 6 and EMISSION.fullmatch(lines[0]) is not None
            and INSTITUTION.search(lines[2]) is not None
            and "Agência:" in lines[2] and "Conta:" in lines[2] and "R$" not in lines[2]
            and PERIOD.fullmatch(lines[3]) is not None and lines[4] == SUMMARY_HEADER)


def parse_inter_money(value: str) -> Decimal:
    if re.fullmatch(MONEY, value) is None:
        raise StatementParseError("Invalid Inter monetary value.")
    number = value.removeprefix("-").removeprefix("R$").strip()
    amount = Decimal(number.replace(".", "").replace(",", "."))
    return amount.copy_negate() if value.startswith("-") else amount


def _calendar_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as exc:
        raise StatementParseError("Invalid Inter calendar date.") from exc


def _day_date(match: re.Match[str]) -> date:
    try:
        return date(int(match[3]), MONTHS[match[2].lower()], int(match[1]))
    except (ValueError, KeyError) as exc:
        raise StatementParseError("Invalid Inter group date.") from exc


def _page_lines(text: str, number: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if (len(lines) < 2 or lines[-2] != "Fale com a gente"
            or re.search(r"\bSAC\b", lines[-1]) is None
            or re.search(r"\bOuvidoria\b", lines[-1]) is None
            or "R$" in lines[-1] or re.search(NUMBER, lines[-1])):
        raise StatementParseError(f"Missing or unsupported Inter footer on page {number}.")
    return lines[:-2]


def _check_day(transactions: list[Transaction], start: int, expected: Decimal | None) -> None:
    if expected is None:
        return
    if len(transactions) == start:
        raise StatementParseError("Inter date group contains no transactions.")
    if transactions[-1].balance_after != expected:
        raise StatementValidationError("Inter daily closing balance differs from its last transaction.")


class InterParser:
    bank_name = "Banco Inter"
    layout_id = "inter-digital-v1"

    def parse(self, document: ExtractedDocument) -> Statement:
        if not matches_layout(document):
            raise StatementParseError("Document does not match the supported Inter layout.")
        pages = [_page_lines(page.text, page.number) for page in document.pages]
        first = pages[0]
        if len(first) < 7 or EMISSION.fullmatch(first[0]) is None:
            raise StatementParseError("Incomplete Inter statement header.")
        if "R$" in first[1]:
            raise StatementParseError("Unexpected monetary content in Inter customer header.")
        account_match = ACCOUNT.fullmatch(first[2])
        period = PERIOD.fullmatch(first[3])
        summary = SUMMARY.fullmatch(first[5])
        if account_match is None or period is None or first[4] != SUMMARY_HEADER or summary is None:
            raise StatementParseError("Missing or malformed Inter account, period or balance header.")
        closing, _, _ = (parse_inter_money(summary[index]) for index in (1, 2, 3))
        # Remove only the documented separator. Any other line remains subject
        # to the body grammar, including malformed monetary rows.
        body_start = 7 if first[6] == "(bloqueado + disponível)" else 6
        pages[0] = first[body_start:]
        transactions: list[Transaction] = []
        current_date: date | None = None
        daily_balance: Decimal | None = None
        group_start = 0
        for lines in pages:
            for line in lines:
                day = DAY.fullmatch(line)
                if day is not None:
                    _check_day(transactions, group_start, daily_balance)
                    current_date = _day_date(day)
                    daily_balance = parse_inter_money(day[4])
                    group_start = len(transactions)
                    continue
                movement = MOVEMENT.fullmatch(line)
                if movement is None or current_date is None:
                    raise StatementParseError("Malformed or unexpected Inter transaction/group line.")
                description = movement[1]
                if "R$" in description:
                    raise StatementParseError("Ambiguous extra monetary column in Inter transaction.")
                transactions.append(Transaction(
                    posting_date=current_date, description=description,
                    amount=parse_inter_money(movement[2]), balance_after=parse_inter_money(movement[3]),
                ))
        _check_day(transactions, group_start, daily_balance)
        return Statement(
            bank_id="inter", layout_id=self.layout_id,
            period_start=_calendar_date(period[1]), period_end=_calendar_date(period[2]),
            opening_balance=None, closing_balance=closing, transactions=tuple(transactions),
            chronology=Chronology.ASCENDING, running_balances_required=True,
            currency=Currency("BRL"),
            account=BankAccount(
                organization="Banco Inter", institution_id="077", bank_id="077",
                # In this profile BRANCHID retains its check-digit separator;
                # ACCTID uses digits only. Never strip zero padding.
                branch_id=account_match[1],
                account_id=account_match[2].replace("-", ""),
            ),
        )

    def interpret(self, document: ExtractedDocument) -> Interpretation:
        """Keep the proven grammar; add index-only sources for its known roles."""
        statement = self.parse(document)
        lines = text_lines(document)
        money = re.compile(rf"{MONEY}|{NUMBER}")
        expected = tuple(text_span(page, row, text, match.start(), match.end())
                         for page, row, text in lines for match in money.finditer(text))
        coverage = CoverageLedger(expected)
        date_source = None
        transaction_index = 0
        for page, row, text in lines:
            regions = list(money.finditer(text))
            if page == document.pages[0].number and row == 6:
                for index, region in enumerate(regions):
                    coverage.claim(text_span(page, row, text, region.start(), region.end()),
                                   FinancialRole.CLOSING_BALANCE if index == 0 else FinancialRole.BALANCE_COMPONENT)
            elif (day := DAY.fullmatch(text)) is not None:
                date_source = text_span(page, row, text, 0, text.index(" Saldo"))
                region = regions[0]
                coverage.claim(text_span(page, row, text, region.start(), region.end()), FinancialRole.DAILY_BALANCE)
            elif (movement := MOVEMENT.fullmatch(text)) is not None:
                for group, role in ((2, FinancialRole.MOVEMENT), (3, FinancialRole.RUNNING_BALANCE)):
                    coverage.claim(text_span(page, row, text, *movement.span(group)), role, transaction_index)
                assert date_source is not None
                coverage.transaction(date_source, text_span(page, row, text))
                coverage.field_sources(transaction_index, date=date_source,
                    description=(text_span(page, row, text, *movement.span(1)),),
                    amount=text_span(page, row, text, *movement.span(2)),
                    balance=text_span(page, row, text, *movement.span(3)),
                    direction=text_span(page, row, text, *movement.span(2)),
                    direction_basis="explicit_sign" if movement[2].startswith("-") else "unsigned_credit_convention",
                    economic_order=transaction_index)
                transaction_index += 1
        evidence = replace(validate_statement(statement),
                           daily_balances_verified=EvidenceStatus.VERIFIED,
                           group_subtotals_verified=EvidenceStatus.NOT_APPLICABLE)
        return coverage.finish(statement, evidence)
