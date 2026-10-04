"""Explicit financial scopes and informational frames, with complete row ownership.

M7 exposes conservative single-scope segmentation. Callers may provide an
explicit partition; multiple financial scopes require selection in future work.
No section containing money can disappear as an informational frame.
"""

from dataclasses import dataclass

from pdf_to_ofx.domain.evidence import DocumentRegion
from pdf_to_ofx.domain.errors import FinancialCoverageError
from pdf_to_ofx.generic.operators.candidates import OperatorFailure, _period
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.semantics import has_financial_signal
from pdf_to_ofx.generic.structure import Row


@dataclass(frozen=True, slots=True)
class FinancialScope:
    region: DocumentRegion
    rows: tuple[Row, ...]


def segment_scopes(original: tuple[Row, ...], retained: tuple[Row, ...],
                   coverage: VisualCoverage,
                   partitions: tuple[tuple[str, tuple[Row, ...]], ...] | None = None,
                   ) -> tuple[tuple[FinancialScope, ...], tuple[DocumentRegion, ...]]:
    partitions = partitions if partitions is not None else (("main", retained),)
    original_ids = {id(row) for row in original}
    retained_ids = {id(row) for row in retained}
    assigned = [id(row) for _, rows in partitions for row in rows]
    if (len(assigned) != len(set(assigned)) or set(assigned) != retained_ids
            or not retained_ids <= original_ids
            or len({name for name, _ in partitions}) != len(partitions)
            or any(not name or not rows for name, rows in partitions)):
        raise OperatorFailure("scope_segmentation", "Financial scopes require a complete disjoint row partition.")
    coverage.bind_scopes(partitions)
    scopes = tuple(FinancialScope(DocumentRegion(name, "financial",
        tuple(coverage.span(row) for row in rows)), rows) for name, rows in partitions)
    removed = tuple(row for row in original if id(row) not in retained_ids)
    removed_indexes = {coverage.row_indexes[id(row)] for row in removed}
    if any((source.page, source.row) in removed_indexes for source in coverage.expected):
        raise FinancialCoverageError("Unclassified monetary regions were removed from the financial scope.")
    if any(has_financial_signal(row.text) and not _period(row) for row in removed):
        raise OperatorFailure("scope_segmentation", "Financial content cannot belong to an informational frame.")
    coverage.region_ids.update({id(row): "page_frames" for row in removed})
    informational = (DocumentRegion("page_frames", "informational",
                     tuple(coverage.span(row) for row in removed)),) if removed else ()
    return scopes, informational
