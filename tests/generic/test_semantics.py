from datetime import date
from decimal import Decimal, localcontext

import pytest

from pdf_to_ofx.generic.semantics import balance_labels, parse_date, parse_money


@pytest.mark.parametrize(("text", "expected"), [
    ("01/03/2027", date(2027, 3, 1)), ("1 de março de 2027", date(2027, 3, 1)),
    ("29 de FEVEREIRO de 2028", date(2028, 2, 29)),
    ("31/02/2027", None), ("1 de inexistente de 2027", None), ("CLIENTE FICTÍCIO", None),
])
def test_exact_calendar_dates(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(("text", "expected", "marker"), [
    ("R$ 1.500,00", "1500.00", None), ("1.500,00", "1500.00", None),
    ("-R$ 35,00", "-35.00", None), ("-35,00", "-35.00", None),
    ("1.500,00 C", "1500.00", "C"), ("35,00 D", "-35.00", "D"),
    ("+R$ 0,01", "0.01", None), ("- R$ 0,01", "-0.01", None),
])
def test_money_signs_and_exact_cents(text, expected, marker):
    with localcontext() as context:
        context.prec = 2
        result = parse_money(text)
        assert result is not None and result.amount == Decimal(expected) and result.marker == marker


@pytest.mark.parametrize("text", [
    "PAGAMENTO JOÃO", "1.23,45", "1,001", "R$ BAD", "12.50", "-35,00 C", "+35,00 D",
])
def test_nonfinancial_or_conflicting_money_is_rejected(text):
    assert parse_money(text) is None


def test_balance_roles_are_financial_concepts():
    assert balance_labels("Saldo inicial: Saldo final: Saldo do dia:") == ("inicial", "final", "do dia")
    assert balance_labels("Cliente fictício sem valores") == ()
