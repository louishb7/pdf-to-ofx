"""Exact Portuguese dates and Brazilian money, without institution grammars."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from unicodedata import category

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


@dataclass(frozen=True, slots=True)
class MoneyScan:
    """Spatial candidates and unresolved expressions, in original word indexes.

    An inventory is usable only when ``incomplete`` is empty. A detached affix
    can invalidate the row without binding it across the token-gap tolerance.
    """
    regions: tuple[MoneyRegion, ...]
    incomplete: tuple[tuple[int, int], ...]


def scan_money_regions(row: Row, tolerances: Tolerances) -> MoneyScan:
    """Do not expose a valid suffix of an unsupported monetary expression.

    Integers alone are not money. A nearby numeric fragment or affix, however,
    must not disappear when scanning a complete decimal candidate. Explicit
    signs/currency delimit amounts from preceding description references.
    """
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
    covered = {i for region in regions for i in range(region.start, region.end)}
    incomplete = []
    complete = []

    def adjacent(left: int, right: int) -> bool:
        return row.words[right].x0 - row.words[left].x1 <= tolerances.token_gap

    def affix(index: int) -> bool:
        text = row.words[index].text
        # These are refusal cues, not new supported currencies/directions.
        return (text in {"+", "-", "−", "(", ")", "R$", "C", "D"}
                or any(category(char) == "Sc" for char in text)
                or re.fullmatch(r"[+−-]?[0-9.,_'’]+", text) is not None
                or re.fullmatch(r"[A-Z]{2,3}", text) is not None)

    for region in regions:
        start, end = region.start, region.end
        explicit_start = region.money.explicit_sign or row.words[start].text.startswith("R$")
        while start and start - 1 not in covered and adjacent(start - 1, start):
            previous = start - 1
            # A separated reference before +amount/R$amount is description.
            touching = row.words[start].x0 <= row.words[previous].x1
            reference = re.fullmatch(r"[0-9.,_'’]+|[A-Z]{2,3}", row.words[previous].text) is not None
            if not touching and (not affix(previous) or explicit_start and reference):
                break
            start = previous
        while end < len(row.words) and end not in covered and adjacent(end - 1, end):
            touching = row.words[end].x0 <= row.words[end - 1].x1
            if not touching and not affix(end):
                break
            end += 1
        if (start, end) != (region.start, region.end):
            incomplete.append((start, end))
        else:
            complete.append(region)
        # Outside token_gap this is not a binding. Keep the spatial candidate,
        # but reject an inventory that would leave its sign/currency unconsumed.
        if region.start and region.start - 1 not in covered:
            prefix = row.words[region.start - 1].text
            if prefix in {"+", "-", "−", "R$"} or any(category(char) == "Sc" for char in prefix):
                incomplete.append((region.start - 1, end))

    for index, word in enumerate(row.words):
        if index not in covered and re.search(r"[0-9]+,[0-9]+", word.text):
            start, end = index, index + 1
            while start and start - 1 not in covered and adjacent(start - 1, start) and affix(start - 1):
                start -= 1
            while end < len(row.words) and end not in covered and adjacent(end - 1, end) and affix(end):
                end += 1
            incomplete.append((start, end))
    merged = []
    for start, end in sorted(set(incomplete)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return MoneyScan(tuple(complete), tuple(merged))


def money_regions(row: Row, tolerances: Tolerances) -> tuple[MoneyRegion, ...]:
    return scan_money_regions(row, tolerances).regions


def has_financial_signal(text: str) -> bool:
    # Also flag malformed cents/currency so they cannot disappear as prose.
    return any(symbol in text for symbol in ("$", "€", "£")) or re.search(r"[0-9]+,[0-9]+", text) is not None


def balance_labels(text: str) -> tuple[str, ...]:
    return tuple(match[1].casefold() for match in BALANCE_LABEL.finditer(text))
