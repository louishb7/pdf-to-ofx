"""Translate original visual rows to scoped, index-only coverage references."""

from dataclasses import replace

from pdf_to_ofx.domain.evidence import FinancialRole, SourceSpan
from pdf_to_ofx.generic.semantics import MoneyRegion, money_regions
from pdf_to_ofx.generic.structure import Row, Tolerances
from pdf_to_ofx.validation.coverage import CoverageLedger


class VisualCoverage(CoverageLedger):
    def __init__(self, rows: tuple[Row, ...], tolerances: Tolerances) -> None:
        self.row_indexes: dict[int, tuple[int, int]] = {}
        self.region_ids: dict[int, str] = {}
        counts: dict[int, int] = {}
        expected = []
        for row in rows:
            counts[row.page] = counts.get(row.page, 0) + 1
            self.row_indexes[id(row)] = (row.page, counts[row.page])
            expected.extend(self.span(row, region.start, region.end) for region in money_regions(row, tolerances))
        super().__init__(tuple(expected))

    def span(self, row: Row, start: int = 0, end: int | None = None) -> SourceSpan:
        page, index = self.row_indexes[id(row)]
        return SourceSpan(page, index, start, len(row.words) if end is None else end,
                          region_id=self.region_ids.get(id(row), "main"))

    def bind_scopes(self, partitions: tuple[tuple[str, tuple[Row, ...]], ...]) -> None:
        if self.assignments or self.transactions:
            raise ValueError("Scope binding must precede financial ownership.")
        self.region_ids = {id(row): name for name, rows in partitions for row in rows}
        source_scopes = {self.row_indexes[id(row)]: name for name, rows in partitions for row in rows}
        self.expected = frozenset(replace(source, region_id=source_scopes.get((source.page, source.row), "main"))
                                  for source in self.expected)

    def monetary(self, row: Row, region: MoneyRegion, role: FinancialRole,
                 transaction_index: int | None = None) -> None:
        self.claim(self.span(row, region.start, region.end), role, transaction_index)


def canonical_sources(sources: tuple[SourceSpan, ...]) -> tuple[SourceSpan, ...]:
    """Merge adjacent token intervals; preserve gaps, order and occurrences."""
    output = []
    for source in sources:
        if output:
            previous = output[-1]
            if ((previous.page, previous.row, previous.basis, previous.region_id, previous.word_end) ==
                    (source.page, source.row, source.basis, source.region_id, source.word_start)):
                output[-1] = replace(previous, word_end=source.word_end)
                continue
        output.append(source)
    return tuple(output)
