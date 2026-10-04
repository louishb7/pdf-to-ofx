"""Document order is retained; economic order needs calendar evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pdf_to_ofx.domain.evidence import ChronologySource, SourceSpan
from pdf_to_ofx.domain.models import Chronology, Transaction

if TYPE_CHECKING:
    from pdf_to_ofx.generic.composition import CompositionInput
    from pdf_to_ofx.generic.operators.amounts import AmountRoles


def chronology_candidates(prepared: CompositionInput, amounts: tuple[tuple[AmountRoles, ...], ...]) -> tuple[Chronology, ...]:
    running = sum(any(role.running_balance for role in domain) for domain in amounts)
    boundary = (prepared.context.opening_balance is not None or prepared.context.closing_balance is not None
                or bool(prepared.monetary_domains))
    checkpoints = any(c.kind == "checkpoint" or c.kind == "date_heading" and c.amounts for c in prepared.candidates)
    return (Chronology.ASCENDING, Chronology.DESCENDING) if running >= 2 or running and boundary or checkpoints else (Chronology.ASCENDING,)


def infer_chronology(transactions: tuple[Transaction, ...], sources: tuple[SourceSpan, ...],
                     ) -> tuple[Chronology, ChronologySource]:
    order = tuple(range(len(transactions)))
    ascending = all(a.posting_date <= b.posting_date for a, b in zip(transactions, transactions[1:]))
    return (Chronology.ASCENDING if ascending else Chronology.UNDECLARED,
            ChronologySource(order, order if ascending else None,
                             "nondecreasing_full_dates" if ascending else "economic_order_unresolved", sources))
