"""Bind wrapped description punctuation using columns and signed flow owners."""

from pdf_to_ofx.generic.operators.candidates import _flow, date_candidates
from pdf_to_ofx.generic.semantics import has_financial_signal, scan_money_regions
from pdf_to_ofx.generic.structure import Row, Tolerances


def description_separators(rows: tuple[Row, ...], tolerances: Tolerances) -> set[tuple[int, int, int]]:
    """Recognize a trailing separator in a distinct, continuing description cell.

    A distant sign alone is insufficient. Require a signed flow owner, a
    three-column row, repeated punctuation within its middle cell, and a
    following line aligned to that cell. No monetary token is removed; the
    separator remains in the description and its original provenance.
    """
    output = set()
    flow_page = None
    for index, row in enumerate(rows):
        if _flow(row, tolerances) is not None:
            flow_page = row.page
            continue
        if date_candidates(row):
            flow_page = None
        if flow_page != row.page or index + 1 == len(rows):
            continue
        scan = scan_money_regions(row, tolerances)
        if len(scan.regions) != 1 or not scan.incomplete:
            continue
        amount = scan.regions[0]
        following = rows[index + 1]
        if (following.page != row.page or date_candidates(following)
                or has_financial_signal(following.text)
                or following.words[0].top - row.words[-1].bottom > tolerances.summary_y
                or following.words[-1].x1 >= amount.x0 - tolerances.token_gap):
            continue
        left = following.words[0].x0
        cell = next((i for i, word in enumerate(row.words)
                     if abs(word.x0 - left) <= tolerances.row_y), None)
        sign = amount.start - 1
        if (cell is None or cell == 0 or sign <= cell + 1
                or row.words[cell].x0 - row.words[cell - 1].x1 <= tolerances.token_gap
                or row.words[sign].text != "-"
                or row.words[sign].x0 - row.words[sign - 1].x1 > tolerances.token_gap
                or amount.x0 - row.words[sign].x1 <= tolerances.token_gap
                or not any(w.text == "-" for w in row.words[cell:sign - 1])
                or amount.money.explicit_sign or amount.money.marker):
            continue
        output.add((id(row), sign, amount.end))
    return output
