from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

import pytest

from pdf_to_ofx.banks.detector import detect_parser
from pdf_to_ofx.banks.inter import InterParser, parse_inter_money
from pdf_to_ofx.domain.errors import StatementParseError, StatementValidationError, UnsupportedLayoutError
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage
from pdf_to_ofx.validation.statement import validate_statement


def document(pages: tuple[str, ...]) -> ExtractedDocument:
    return ExtractedDocument(tuple(ExtractedPage(index, text) for index, text in enumerate(pages, 1)))


def test_detects_strong_inter_layout(inter_document: ExtractedDocument) -> None:
    assert isinstance(detect_parser(inter_document), InterParser)


def test_emission_hour_can_have_one_digit(inter_pages: tuple[str, ...]) -> None:
    pages = tuple(page.replace("10h00", "8h05") for page in inter_pages)
    assert isinstance(detect_parser(document(pages)), InterParser)
    assert len(InterParser().parse(document(pages)).transactions) == 5


def test_bank_mention_in_body_cannot_replace_institution_header(inter_pages: tuple[str, ...]) -> None:
    pages = tuple(page.replace("Banco Inter |", "Outro banco fictício |")
                  .replace("CRÉDITO FICTÍCIO ALFA", "CRÉDITO Banco Inter FICTÍCIO") for page in inter_pages)
    with pytest.raises(UnsupportedLayoutError):
        detect_parser(document(pages))


def test_account_metadata_preserves_leading_zeroes(inter_pages: tuple[str, ...]) -> None:
    pages = tuple(page.replace("9999-9,", "0099-9,").replace("9999999-9", "0099999-9") for page in inter_pages)
    statement = InterParser().parse(document(pages))
    assert statement.account is not None
    assert statement.account.branch_id == "0099-9"
    assert statement.account.account_id == "00999999"


@pytest.mark.parametrize("text", ["Banco Inter S.A.", "Transferência Banco Inter S.A. R$ 1,00 R$ 2,00"])
def test_institution_mention_alone_cannot_detect_bank(text: str) -> None:
    with pytest.raises(UnsupportedLayoutError):
        detect_parser(document((text,)))


def test_normalized_fields_and_page_context(inter_statement: Statement) -> None:
    statement = inter_statement
    assert statement.bank_id == "inter"
    assert statement.layout_id == "inter-digital-v1"
    assert statement.period_start == date(2027, 2, 1)
    assert statement.period_end == date(2027, 2, 4)
    assert statement.opening_balance is None
    assert statement.closing_balance == Decimal("1000.00")
    assert [t.posting_date for t in statement.transactions] == [
        date(2027, 2, 1), date(2027, 2, 1), date(2027, 2, 2), date(2027, 2, 2), date(2027, 2, 3),
    ]
    assert [t.description for t in statement.transactions] == [
        "CRÉDITO FICTÍCIO ALFA", "DÉBITO FICTÍCIO BETA", "CRÉDITO FICTÍCIO GAMA",
        "DÉBITO FICTÍCIO DELTA", "CRÉDITO FICTÍCIO ÉPSILON",
    ]
    assert [t.amount for t in statement.transactions] == list(map(Decimal, ["250", "-100", "50", "-70", "20"]))
    assert [t.balance_after for t in statement.transactions] == list(map(Decimal, ["1100", "1000", "1050", "980", "1000"]))
    assert statement.account is not None
    assert statement.account.bank_id == statement.account.institution_id == "077"
    assert statement.account.organization == "Banco Inter"
    assert statement.account.branch_id == "9999-9"
    assert statement.account.account_id == "99999999"
    assert statement.account.account_type == "CHECKING"
    validate_statement(statement)


@pytest.mark.parametrize(("text", "expected"), [
    ("R$ 0,01", "0.01"), ("-R$ 35,00", "-35.00"),
    ("R$ 1.234,56", "1234.56"), ("-R$ 1.234.567,89", "-1234567.89"),
])
def test_signed_brazilian_money(text: str, expected: str) -> None:
    with localcontext() as ctx:
        ctx.prec = 2
        assert parse_inter_money(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["R$ 01,00", "R$ 1.00,00", "R$ 1,001", "R$ 1,0", "35,00 D", "R$ NaN", "R$ -35,00"])
def test_invalid_money_fails(text: str) -> None:
    with pytest.raises(StatementParseError):
        parse_inter_money(text)


@pytest.mark.parametrize(("old", "new"), [
    ("-R$ 100,00", "-R$ 100,xx"), ("-R$ 100,00", "-R$ 100,00 R$ 5,00"),
    ("-R$ 100,00 R$ 1.000,00", "-R$ 100,00"),
    ("DÉBITO FICTÍCIO BETA", ""), ("01 de fevereiro", "31 de fevereiro"),
    ("01 de fevereiro", "01 de inexistente"), ("01/02/2027", "31/02/2027"),
    ("Agência: 9999-9", "Agência: desconhecida"),
    ("Período:", "Intervalo:"), ("Saldo bloqueado", "Saldo desconhecido"),
    ("(bloqueado + disponível)", "CONTEÚDO NÃO RECONHECIDO"),
    ("CRÉDITO FICTÍCIO ALFA", "LINHA INESPERADA\nCRÉDITO FICTÍCIO ALFA"),
    ("Fale com a gente", "RODAPÉ DESCONHECIDO"),
])
def test_malformed_or_unexpected_lines_fail(inter_pages: tuple[str, ...], old: str, new: str) -> None:
    pages = tuple(page.replace(old, new) for page in inter_pages)
    with pytest.raises(StatementParseError):
        InterParser().parse(document(pages))


def test_movement_before_date_is_not_guessed(inter_pages: tuple[str, ...]) -> None:
    pages = (inter_pages[0].replace("01 de fevereiro de 2027 Saldo do dia: R$ 1.000,00 Valor Saldo por transação\n", ""), inter_pages[1])
    with pytest.raises(StatementParseError):
        InterParser().parse(document(pages))


def test_empty_date_group_is_rejected(inter_pages: tuple[str, ...]) -> None:
    pages = (inter_pages[0], inter_pages[1].replace("CRÉDITO FICTÍCIO GAMA R$ 50,00 R$ 1.050,00\n", "")
             .replace("DÉBITO FICTÍCIO DELTA -R$ 70,00 R$ 980,00\n", ""))
    with pytest.raises(StatementParseError, match="no transactions"):
        InterParser().parse(document(pages))


@pytest.mark.parametrize(("old", "new"), [
    ("SAC:", "Serviço:"),
    ("Ouvidoria:", "Contato:"),
    ("SAC: atendimento fictício", "SAC: atendimento fictício R$ 5,00"),
])
def test_footer_cannot_hide_monetary_rows(inter_pages: tuple[str, ...], old: str, new: str) -> None:
    pages = tuple(page.replace(old, new) for page in inter_pages)
    with pytest.raises(StatementParseError, match="footer"):
        InterParser().parse(document(pages))


def test_daily_closing_mismatch_fails(inter_pages: tuple[str, ...]) -> None:
    pages = tuple(page.replace("Saldo do dia: R$ 980,00", "Saldo do dia: R$ 981,00") for page in inter_pages)
    with pytest.raises(StatementValidationError, match="daily"):
        InterParser().parse(document(pages))


def test_running_balance_mismatch_fails_without_opening(inter_statement: Statement) -> None:
    altered = replace(inter_statement.transactions[2], balance_after=Decimal("1050.01"))
    invalid = replace(inter_statement, transactions=(*inter_statement.transactions[:2], altered, *inter_statement.transactions[3:]))
    with pytest.raises(StatementValidationError, match="Running balance"):
        validate_statement(invalid)


def test_closing_mismatch_fails_even_without_opening(inter_statement: Statement) -> None:
    with pytest.raises(StatementValidationError, match="closing balance"):
        validate_statement(replace(inter_statement, closing_balance=Decimal("1000.01")))


@pytest.mark.parametrize("balance", [None, Decimal("NaN"), Decimal("1000.001"), 1000.0])
def test_missing_or_invalid_running_balance_fails(inter_statement: Statement, balance: object) -> None:
    first = replace(inter_statement.transactions[0], balance_after=balance)
    with pytest.raises(StatementValidationError):
        validate_statement(replace(inter_statement, transactions=(first, *inter_statement.transactions[1:])))


def test_inter_cannot_drop_all_running_balances(inter_statement: Statement) -> None:
    invalid = replace(inter_statement, transactions=tuple(replace(t, balance_after=None) for t in inter_statement.transactions))
    with pytest.raises(StatementValidationError):
        validate_statement(invalid)


def test_known_opening_balance_checks_first_movement(inter_statement: Statement) -> None:
    validate_statement(replace(inter_statement, opening_balance=Decimal("850.00")))
    with pytest.raises(StatementValidationError):
        validate_statement(replace(inter_statement, opening_balance=Decimal("850.01")))


def test_low_precision_cannot_hide_running_mismatch(inter_statement: Statement) -> None:
    with localcontext() as ctx:
        ctx.prec = 2
        validate_statement(inter_statement)
        with pytest.raises(StatementValidationError):
            validate_statement(replace(inter_statement, closing_balance=Decimal("1000.01")))


def test_no_silent_sorting_or_empty_statement(inter_statement: Statement) -> None:
    for transactions in ((), inter_statement.transactions[::-1]):
        with pytest.raises(StatementValidationError):
            validate_statement(replace(inter_statement, transactions=transactions))


def test_dates_outside_period_fail(inter_statement: Statement) -> None:
    with pytest.raises(StatementValidationError, match="period"):
        validate_statement(replace(inter_statement, period_end=date(2027, 2, 2)))
