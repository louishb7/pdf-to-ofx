"""Exact region ownership for the existing grammars, independent of balances."""

from dataclasses import replace

from pdf_to_ofx.domain.errors import FinancialCoverageError
from pdf_to_ofx.domain.evidence import (
    EvidenceReport, EvidenceStatus, FinancialRole, Interpretation,
    MonetaryAssignment, Provenance, SourceSpan, TransactionSource,
)
from pdf_to_ofx.domain.models import Statement


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
        self.assignments: dict[SourceSpan, MonetaryAssignment] = {}
        self.transactions: list[list[SourceSpan]] = []

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

    def finish(self, statement: Statement, evidence: EvidenceReport) -> Interpretation:
        if self.expected != self.assignments.keys():
            raise FinancialCoverageError("Unclassified monetary regions remain in the document.")
        movements = [a.transaction_index for a in self.assignments.values()
                     if a.role == FinancialRole.MOVEMENT]
        if (len(self.transactions) != len(statement.transactions)
                or sorted(movements, key=lambda i: -1 if i is None else i) != list(range(len(statement.transactions)))
                or any(not spans for spans in self.transactions)):
            raise FinancialCoverageError("Transaction occurrences and movement sources disagree.")
        provenance = Provenance(
            tuple(TransactionSource(i, tuple(spans)) for i, spans in enumerate(self.transactions)),
            tuple(self.assignments[source] for source in sorted(self.expected,
                  key=lambda s: (s.page, s.row, s.word_start, s.word_end))),
        )
        return Interpretation(statement, replace(evidence, financial_coverage_verified=EvidenceStatus.VERIFIED), provenance)
