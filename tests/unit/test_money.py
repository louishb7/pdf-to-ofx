from decimal import Decimal, localcontext

import pytest

from pdf_to_ofx.banks.synthetic import parse_brazilian_money
from pdf_to_ofx.domain.errors import StatementParseError


@pytest.mark.parametrize(("text", "expected"), [
    ("R$ 500,00 C", "500.00"), ("R$ 35,00 D", "-35.00"),
    ("R$ 1.000,00 C", "1000.00"), ("R$ 1.234.567,89 D", "-1234567.89"),
    ("R$ 0,01 C", "0.01"), ("R$ 0,99 D", "-0.99"),
    ("R$ 0,00 C", "0.00"), ("R$ 1234,56 C", "1234.56"),
])
def test_explicit_money_parsing(text: str, expected: str) -> None:
    assert parse_brazilian_money(text) == Decimal(expected)


@pytest.mark.parametrize("text", [
    "", "R$ 35,00", "R$ 35,00 X", "R$ -35,00 D", "R$ +35,00 C", "R$ 1,000.00 C",
    "R$ 1.00,00 C", "R$ 01,00 C", "R$ 1,0 C", "R$ 1,001 C", "R$ NaN C", "R$ Infinity D",
    "R$ 35.00 D", " R$ 35,00 D", "R$ 35,00 D extra", "R$ 35,00 c", "R$ ３５,00 D",
    "35,00 D", "1.000,00 C",
])
def test_invalid_money_is_rejected(text: str) -> None:
    with pytest.raises(StatementParseError, match="monetary"):
        parse_brazilian_money(text)


def test_debit_parsing_does_not_round_under_low_decimal_precision() -> None:
    with localcontext() as context:
        context.prec = 2
        assert parse_brazilian_money("R$ 1.234.567,89 D") == Decimal("-1234567.89")
