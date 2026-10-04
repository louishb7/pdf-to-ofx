"""Translate existing visual rows to index-only coverage references."""

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.generic.semantics import MoneyRegion, money_regions
from pdf_to_ofx.generic.structure import Row, Tolerances
from pdf_to_ofx.validation.coverage import CoverageLedger


class VisualCoverage(CoverageLedger):
    def __init__(self, rows: tuple[Row, ...], tolerances: Tolerances) -> None:
        self.row_indexes: dict[int, tuple[int, int]] = {}
        counts: dict[int, int] = {}
        expected = []
        for row in rows:
            counts[row.page] = counts.get(row.page, 0) + 1
            self.row_indexes[id(row)] = (row.page, counts[row.page])
            expected.extend(self.span(row, region.start, region.end) for region in money_regions(row, tolerances))
        super().__init__(tuple(expected))

    def span(self, row: Row, start: int = 0, end: int | None = None) -> SourceSpan:
        page, index = self.row_indexes[id(row)]
        return SourceSpan(page, index, start, len(row.words) if end is None else end)

    def monetary(self, row: Row, region: MoneyRegion, role: FinancialRole,
                 transaction_index: int | None = None) -> None:
        self.claim(self.span(row, region.start, region.end), role, transaction_index)
