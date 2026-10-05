"""Observe supported frames and columns; never select a financial outcome."""

from dataclasses import replace
from itertools import groupby
import re

from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.generic.operators.candidates import _flow, _period, _stamp, leading_date
from pdf_to_ofx.generic.operators.pages import contact_footer_rows
from pdf_to_ofx.generic.operators.transactions import semantic_candidates
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import has_financial_signal, money_regions
from pdf_to_ofx.generic.structure import Row, Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


def observe_geometry(document: ExtractedDocument, supplied: LayoutProfile | None, tolerances: Tolerances) -> LayoutProfile:
    if supplied:
        return supplied
    rows = reconstruct_rows(document, tolerances)
    framed = observe_grouped_geometry(rows, tolerances)
    if framed:
        return framed
    base = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING,
                         footer_rows=contact_footer_rows(rows), tolerances=tolerances)
    candidates = semantic_candidates(rows, base)
    start = next((c.position for c in candidates if c.dates and
                  (c.kind in {"date_heading", "flow"} or c.kind == "content" and c.amounts)), len(candidates))
    movements = [c for c in candidates[start:] if c.kind == "content" and c.amounts]
    grouped = any(c.kind in {"date_heading", "flow"} and c.dates for c in candidates[start:])
    flow = any(c.kind == "flow" for c in candidates[start:])
    markers = any(r.money.marker for c in movements for r in c.amounts)
    mode = AmountMode.GROUP_SUBTOTAL if flow and all(len(c.amounts) == 1 and
        not c.amounts[0].money.explicit_sign and not c.amounts[0].money.marker for c in movements) else (
            AmountMode.CREDIT_DEBIT_MARKER if markers else AmountMode.SIGNED)
    balance = BalanceMode.RUNNING if any(len(c.amounts) == 2 for c in movements) else BalanceMode.ABSENT
    return replace(base, date_mode=DateMode.GROUPED if grouped else DateMode.PER_TRANSACTION,
                   amount_mode=mode, balance_mode=balance, balance_column=1 if balance == BalanceMode.RUNNING else None,
                   carry_date_across_pages=grouped, transaction_left=min((c.row.words[0].x0 for c in movements), default=0)
                   if mode == AmountMode.GROUP_SUBTOTAL else None)
def observe_grouped_geometry(rows: tuple[Row, ...], tolerances: Tolerances) -> LayoutProfile | None:
    """Observe the established flow grammar's frames and description boundaries.

    This observation used to live in the legacy grouped parser. Both paths now
    consume it; no amount, reconciliation or institutional identity selects it.
    """
    try:
        pages = [list(group) for _, group in groupby(rows, key=lambda r: r.page)]
        footer = 0
        if len(pages) > 1 and all(_stamp(page[-1]) for page in pages):
            footer = 1
            while all(len(page) > footer and page[-footer - 1].text == pages[0][-footer - 1].text for page in pages):
                footer += 1
            texts = " ".join(r.text for r in pages[0][-footer:-1]).casefold()
            if not re.search(r"\b(?:sac|ouvidoria|atendimento|contato)\b", texts):
                return None
            pages = [page[:-footer] for page in pages]
        header = 0
        if len(pages) > 1:
            while all(len(page) > header and page[header].text == pages[0][header].text for page in pages):
                if not _period(pages[0][header]) and (leading_date(pages[0][header])
                        or has_financial_signal(pages[0][header].text)):
                    break
                header += 1
        body = [row for index, page in enumerate(pages) for row in (page[header:] if index else page)]
        start = next((i for i, row in enumerate(body) if not _period(row) and leading_date(row)), None)
        if start is None or not any(_flow(row, tolerances) for row in body[start:]):
            return None
        movements = [(i, row) for i, row in enumerate(body[start:], start)
                     if money_regions(row, tolerances) and not leading_date(row) and not _flow(row, tolerances)]
        if not movements:
            return None
        transaction_left = min(row.words[0].x0 for _, row in movements)
        candidates = [row.words[0].x0 for row in body[start:] if not money_regions(row, tolerances)
                      and not leading_date(row) and row.words[0].x0 > transaction_left + tolerances.row_y]
        continuation_left = min(candidates) if candidates else None
        tail = movements[-1][0] + 1
        while (continuation_left is not None and tail < len(body)
               and abs(body[tail].words[0].x0 - continuation_left) <= tolerances.row_y):
            tail += 1
        notes = len(body) - tail
        return LayoutProfile(DateMode.GROUPED, AmountMode.GROUP_SUBTOTAL, BalanceMode.ABSENT,
                             balance_column=None, footer_rows=footer, tolerances=tolerances,
                             transaction_left=transaction_left, continuation_left=continuation_left,
                             repeated_header_rows=header, trailing_note_rows=notes)
    except (StatementParseError, ValueError, IndexError):
        return None
