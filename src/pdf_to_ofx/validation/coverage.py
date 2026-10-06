"""Exact region ownership for the existing grammars, independent of balances."""

from dataclasses import replace
from collections.abc import Mapping

from pdf_to_ofx.domain.currency import Currency, resolve_currency
from pdf_to_ofx.domain.errors import FinancialCoverageError
from pdf_to_ofx.domain.evidence import (
    ChronologySource, DocumentRegion, EvidenceReport, EvidenceStatus, FinancialRole, Interpretation,
    MonetaryAssignment, Provenance, SourceSpan, TransactionSource,
)
from pdf_to_ofx.domain.models import Chronology, Statement


def source_has_role(source: SourceSpan | None, role: FinancialRole | str,
                    assignments: Mapping[SourceSpan, MonetaryAssignment]) -> bool:
    assignment = assignments.get(source) if source is not None else None
    return assignment is not None and assignment.role == role


class CoverageLedger:
    """Ephemeral collector; only indexes survive in the immutable result.

    Expected regions come from the original document BEFORE frames or candidate
    rows are removed. Grammar branches may claim only their recognized roles.
    There is deliberately no generic ignore role.
    """
    def __init__(self, expected: tuple[SourceSpan, ...]) -> None:
        self.expected = frozenset(expected)
        if len(self.expected) != len(expected):
            raise FinancialCoverageError("Duplicate source monetary regions.")
        ordered = sorted(expected, key=lambda s: (s.page, s.row, s.basis, s.word_start))
        for left, right in zip(ordered, ordered[1:]):
            if ((left.page, left.row, left.basis) == (right.page, right.row, right.basis)
                    and left.word_end > right.word_start):
                raise FinancialCoverageError("Overlapping source monetary regions.")
        self.assignments: dict[SourceSpan, MonetaryAssignment] = {}
        self.transactions: list[list[SourceSpan]] = []
        self.fields: dict[int, dict] = {}
        self.regions: tuple[DocumentRegion, ...] = ()
        self.chronology: ChronologySource | None = None
        # Explicit currency evidence per monetary token; unsymbolized tokens are absent.
        self.currencies: dict[SourceSpan, Currency] = {}

    def observe_currency(self, source: SourceSpan, currency: Currency) -> None:
        if source not in self.expected or self.currencies.get(source, currency) != currency:
            raise FinancialCoverageError("Currency evidence is missing or repeated for a monetary region.")
        self.currencies[source] = currency

    def resolve_currency(self, declared: Currency | None = None) -> Currency | None:
        return resolve_currency(self.currencies.values(), declared)

    def claim(self, source: SourceSpan, role: FinancialRole,
              transaction_index: int | None = None) -> None:
        if source not in self.expected or source in self.assignments or not isinstance(role, FinancialRole):
            raise FinancialCoverageError("Monetary region ownership is missing or repeated.")
        transaction_role = role in {FinancialRole.MOVEMENT, FinancialRole.RUNNING_BALANCE}
        if (transaction_role and (type(transaction_index) is not int or transaction_index < 0)
                or not transaction_role and transaction_index is not None):
            raise FinancialCoverageError("Invalid transaction ownership for monetary role.")
        self.assignments[source] = MonetaryAssignment(source, role, transaction_index)

    def transaction(self, *sources: SourceSpan) -> None:
        self.transactions.append(list(dict.fromkeys(sources)))

    def continuation(self, source: SourceSpan) -> None:
        if not self.transactions:
            raise FinancialCoverageError("A continuation has no transaction source.")
        self.transactions[-1].append(source)
        if (fields := self.fields.get(len(self.transactions) - 1)) is not None:
            fields["description_sources"] = (*fields["description_sources"], source)

    def field_sources(self, index: int, *, date: SourceSpan,
                      description: tuple[SourceSpan, ...], amount: SourceSpan,
                      balance: SourceSpan | None = None, direction: SourceSpan | None = None,
                      direction_basis: str | None = None,
                      economic_order: int | None = None) -> None:
        if type(index) is not int or index in self.fields or not 0 <= index < len(self.transactions):
            raise FinancialCoverageError("Repeated or missing transaction field ownership.")
        self.fields[index] = dict(date_source=date, description_sources=description,
            amount_source=amount, balance_source=balance, direction_source=direction,
            direction_basis=direction_basis, document_order=index, economic_order=economic_order)

    def finish(self, statement: Statement, evidence: EvidenceReport) -> Interpretation:
        if self.expected != self.assignments.keys():
            raise FinancialCoverageError("Unclassified monetary regions remain in the document.")
        # A declared currency may agree with, never override, document evidence.
        statement = replace(statement, currency=self.resolve_currency(statement.currency))
        for checkpoint in statement.checkpoints:
            if (not source_has_role(checkpoint.source, checkpoint.kind, self.assignments)
                    or self.assignments[checkpoint.source].transaction_index is not None):
                raise FinancialCoverageError("A checkpoint has no exclusive financial source.")
        movements = [a.transaction_index for a in self.assignments.values()
                     if a.role == FinancialRole.MOVEMENT]
        if (len(self.transactions) != len(statement.transactions)
                or sorted(movements, key=lambda i: -1 if i is None else i) != list(range(len(statement.transactions)))
                or any(not spans for spans in self.transactions)):
            raise FinancialCoverageError("Transaction occurrences and movement sources disagree.")
        if self.fields.keys() != set(range(len(statement.transactions))):
            raise FinancialCoverageError("Missing transaction field provenance.")
        balances = [a.transaction_index for a in self.assignments.values() if a.role == FinancialRole.RUNNING_BALANCE]
        expected_balances = [i for i, t in enumerate(statement.transactions) if t.balance_after is not None]
        if sorted(balances) != expected_balances:
            raise FinancialCoverageError("Running balance occurrences and sources disagree.")
        for index, fields in self.fields.items():
            if (fields["balance_source"] is None) != (statement.transactions[index].balance_after is None):
                raise FinancialCoverageError("Missing or unexpected running balance field provenance.")
            if fields["direction_source"] is None or not fields["direction_basis"]:
                raise FinancialCoverageError("Missing movement direction evidence.")
            for field, role in (("amount_source", FinancialRole.MOVEMENT),
                                ("balance_source", FinancialRole.RUNNING_BALANCE)):
                source = fields[field]
                if source is not None and self.assignments.get(source) != MonetaryAssignment(source, role, index):
                    raise FinancialCoverageError("Field source differs from monetary ownership.")
            if not fields["description_sources"]:
                raise FinancialCoverageError("A transaction description has no source.")
            for source in (fields["date_source"], *fields["description_sources"]):
                if any((source.page, source.row, source.basis) == (money.page, money.row, money.basis)
                       and source.word_start < money.word_end and money.word_start < source.word_end
                       for money in self.expected):
                    raise FinancialCoverageError("Date or description overlaps monetary tokens.")
            for source in (fields["date_source"], *fields["description_sources"], fields["amount_source"],
                           fields["balance_source"], fields["direction_source"]):
                if source is not None and not any(
                    (span.page, span.row, span.basis, span.region_id) == (source.page, source.row, source.basis, source.region_id)
                    and span.word_start <= source.word_start < source.word_end <= span.word_end
                    for span in self.transactions[index]):
                    raise FinancialCoverageError("A field source lies outside its transaction evidence regions.")
        regions = self.regions or tuple(DocumentRegion(name, "financial", tuple(dict.fromkeys(
            source for source in (*sorted(self.expected, key=lambda s: (s.page, s.row, s.word_start, s.word_end)),
                                  *(s for spans in self.transactions for s in spans))
            if source.region_id == name))) for name in sorted({s.region_id for s in self.expected}))
        financial = [region for region in regions if region.kind == "financial"]
        if len(financial) != 1 or any(s.region_id != financial[0].region_id for s in self.expected):
            raise FinancialCoverageError("An interpretation requires exactly one coherent financial scope.")
        order = tuple(range(len(statement.transactions)))
        chronology = self.chronology or ChronologySource(order,
            order if statement.chronology == Chronology.ASCENDING else None,
            "declared_ascending" if statement.chronology == Chronology.ASCENDING else "economic_order_unresolved",
            tuple(f["date_source"] for f in self.fields.values()))
        provenance = Provenance(
            tuple(TransactionSource(i, tuple(spans), **self.fields.get(i, {})) for i, spans in enumerate(self.transactions)),
            tuple(self.assignments[source] for source in sorted(self.expected,
                  key=lambda s: (s.page, s.row, s.word_start, s.word_end))),
            regions, chronology,
            tuple(sorted(self.currencies, key=lambda s: (s.page, s.row, s.word_start, s.word_end))),
        )
        return Interpretation(statement, replace(evidence, financial_coverage_verified=EvidenceStatus.VERIFIED), provenance)
