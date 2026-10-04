"""Observe supported frames and columns; never select a financial outcome."""

from dataclasses import replace
from pdf_to_ofx.generic.operators.transactions import semantic_candidates
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


def observe_geometry(document: ExtractedDocument, supplied: LayoutProfile | None, tolerances: Tolerances) -> LayoutProfile:
    if supplied:
        return supplied
    from pdf_to_ofx.generic.grouped import infer_grouped_profile
    from pdf_to_ofx.generic.inference import _contact_footer_rows
    rows = reconstruct_rows(document, tolerances)
    framed = infer_grouped_profile(rows, tolerances)
    if framed:
        return framed
    base = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING,
                         footer_rows=_contact_footer_rows(rows), tolerances=tolerances)
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


