"""Exact Portuguese dates and Brazilian money, without institution grammars."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

from pdf_to_ofx.generic.structure import Row, Tolerances

NUMBER = r"(?:0|[1-9][0-9]{0,2}(?:\.[0-9]{3})*|[1-9][0-9]*),[0-9]{2}"
MONEY = re.compile(rf"(?P<sign>[+-]?)\s*(?:R\$\s*)?(?P<number>{NUMBER})(?:\s+(?P<marker>[CD]))?")
NUMERIC_DATE = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
LONG_DATE = re.compile(r"([0-9]{1,2}) de ([a-zç]+) de ([0-9]{4})", re.IGNORECASE)
SHORT_MONTH_DATE = re.compile(r"([0-9]{1,2}) ([a-zç]{3}) ([0-9]{4})", re.IGNORECASE)
MONTHS = dict(zip(("janeiro", "fevereiro", "março", "abril", "maio", "junho",
                   "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"), range(1, 13)))
BALANCE_LABEL = re.compile(r"\bsaldo\s+(inicial|anterior|final|total|disponível|bloqueado|do dia|diário)\b", re.IGNORECASE)


def parse_date(text: str) -> date | None:
    numeric, written = NUMERIC_DATE.fullmatch(text), LONG_DATE.fullmatch(text)
    abbreviated = SHORT_MONTH_DATE.fullmatch(text)
    try:
        if numeric:
            return date(int(numeric[3]), int(numeric[2]), int(numeric[1]))
        if written:
            return date(int(written[3]), MONTHS[written[2].casefold()], int(written[1]))
        if abbreviated:
            months = {name[:3]: number for name, number in MONTHS.items()}
            return date(int(abbreviated[3]), months[abbreviated[2].casefold()], int(abbreviated[1]))
    except (ValueError, KeyError):
        pass
    return None


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    marker: str | None
    explicit_sign: bool


def parse_money(text: str) -> Money | None:
    match = MONEY.fullmatch(text)
    if match is None:
        return None
    sign, marker = match["sign"], match["marker"]
    if (sign == "-" and marker == "C") or (sign == "+" and marker == "D"):
        return None
    amount = Decimal(match["number"].replace(".", "").replace(",", "."))
    if sign == "-" or marker == "D":
        amount = amount.copy_negate()
    return Money(amount, marker, bool(sign))


@dataclass(frozen=True, slots=True)
class MoneyRegion:
    start: int
    end: int
    money: Money
    x0: float
    x1: float


def money_regions(row: Row, tolerances: Tolerances) -> tuple[MoneyRegion, ...]:
    regions: list[MoneyRegion] = []
    index = 0
    while index < len(row.words):
        found = None
        # At most sign + currency + number + direction marker.
        for end in range(min(index + 4, len(row.words)), index, -1):
            words = row.words[index:end]
            if any(b.x0 - a.x1 > tolerances.token_gap for a, b in zip(words, words[1:])):
                continue
            money = parse_money(" ".join(word.text for word in words))
            if money is not None:
                found = MoneyRegion(index, end, money, words[0].x0, words[-1].x1)
                break
        if found is None:
            index += 1
        else:
            regions.append(found)
            index = found.end
    return tuple(regions)


def has_financial_signal(text: str) -> bool:
    # Also flag malformed cents/currency so they cannot disappear as prose.
    return any(symbol in text for symbol in ("$", "€", "£")) or re.search(r"[0-9]+,[0-9]+", text) is not None


def balance_labels(text: str) -> tuple[str, ...]:
    return tuple(match[1].casefold() for match in BALANCE_LABEL.finditer(text))
