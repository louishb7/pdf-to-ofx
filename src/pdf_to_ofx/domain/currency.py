"""Currency as an explicit financial concept, independent of any locale.

A currency says WHICH unit amounts are denominated in. It deliberately says
nothing about decimal/group separators, symbols, language, country or the way
credits and debits are written: those are lexical conventions (I2/I3).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from pdf_to_ofx.domain.errors import CurrencyConflictError, InvalidCurrencyError


@dataclass(frozen=True, slots=True)
class Currency:
    """Canonical three-letter ISO 4217-shaped code, e.g. ``BRL``.

    The constructor is strict: it never normalizes. Use ``Currency.parse`` for
    external text that may be lowercase or padded. The set of codes is open on
    purpose; any well-formed code is representable.
    """
    code: str

    def __post_init__(self) -> None:
        if (type(self.code) is not str or len(self.code) != 3 or not self.code.isascii()
                or not self.code.isalpha() or not self.code.isupper()):
            raise InvalidCurrencyError("A currency requires a canonical three-letter uppercase code.")

    @classmethod
    def parse(cls, text: str) -> "Currency":
        if type(text) is not str:
            raise InvalidCurrencyError("A currency requires a canonical three-letter uppercase code.")
        return cls(text.strip().upper())

    def __str__(self) -> str:
        return self.code


def resolve_currency(observed: Iterable[Currency], declared: Currency | None = None) -> Currency | None:
    """Single currency of one financial scope, or ``None`` when unproven.

    Distinct currencies fail closed: values are never converted or treated as
    equivalent, and a declaration never overrides contradicting document
    evidence. Absence of evidence is not defaulted to any currency.
    """
    currencies = {value for value in (*observed, declared) if value is not None}
    if len(currencies) > 1:
        raise CurrencyConflictError("Contradictory currencies were observed in one financial scope.")
    return next(iter(currencies), None)
