import pytest
from pdf_to_ofx.domain.currency import Currency, resolve_currency
from pdf_to_ofx.domain.errors import CurrencyConflictError, InvalidCurrencyError

def test_currency_creation_and_normalization():
    assert Currency("BRL").code == "BRL"
    assert Currency("USD").code == "USD"
    assert Currency("EUR").code == "EUR"
    assert Currency.parse(" brl ").code == "BRL"

def test_currency_immutability():
    currency = Currency("BRL")
    with pytest.raises(Exception):
        currency.code = "USD"

@pytest.mark.parametrize("invalid", ["brl", "B1R", "BR", "R$", "", "BRL "])
def test_invalid_currency_constructor(invalid: str):
    with pytest.raises(InvalidCurrencyError):
        Currency(invalid)

@pytest.mark.parametrize("invalid", ["B1R", "BR", "R$", ""])
def test_invalid_currency_parse(invalid: str):
    with pytest.raises(InvalidCurrencyError):
        Currency.parse(invalid)

def test_currency_resolution():
    assert resolve_currency(()) is None
    assert resolve_currency([Currency("BRL")]) == Currency("BRL")
    assert resolve_currency([Currency("BRL"), Currency("BRL")]) == Currency("BRL")
    assert resolve_currency((), Currency("BRL")) == Currency("BRL")
    assert resolve_currency([Currency("BRL")], Currency("BRL")) == Currency("BRL")

def test_currency_conflict():
    with pytest.raises(CurrencyConflictError):
        resolve_currency([Currency("BRL"), Currency("USD")])
    with pytest.raises(CurrencyConflictError):
        resolve_currency([Currency("BRL")], Currency("USD"))
