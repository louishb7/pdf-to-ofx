"""Document order is retained; economic order needs calendar evidence."""

from pdf_to_ofx.domain.evidence import ChronologySource, SourceSpan
from pdf_to_ofx.domain.models import Chronology, Transaction


def infer_chronology(transactions: tuple[Transaction, ...], sources: tuple[SourceSpan, ...],
                     ) -> tuple[Chronology, ChronologySource]:
    order = tuple(range(len(transactions)))
    ascending = all(a.posting_date <= b.posting_date for a, b in zip(transactions, transactions[1:]))
    return (Chronology.ASCENDING if ascending else Chronology.UNDECLARED,
            ChronologySource(order, order if ascending else None,
                             "nondecreasing_full_dates" if ascending else "economic_order_unresolved", sources))
