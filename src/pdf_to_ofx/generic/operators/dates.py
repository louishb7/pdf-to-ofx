"""Date attribution uses only calendar candidates and page/group context."""

from dataclasses import dataclass

from pdf_to_ofx.domain.errors import AmbiguousStatementError
from pdf_to_ofx.generic.operators.candidates import DateCandidate, OperatorFailure
from pdf_to_ofx.generic.operators.transactions import RowCandidate, TransactionSegment
from pdf_to_ofx.generic.profile import DateMode


@dataclass(frozen=True, slots=True)
class DatedSegment:
    segment: TransactionSegment
    candidate: DateCandidate
    source_row: RowCandidate
    group_position: int


def attribute_dates(segments: tuple[TransactionSegment, ...], candidates: tuple[RowCandidate, ...],
                    mode: DateMode, *, carry_across_pages: bool) -> tuple[DatedSegment, ...]:
    output = []
    by_position = {s.position: s for s in segments}
    current: RowCandidate | None = None
    previous_page = None
    for row in candidates:
        if row.row.page != previous_page and not carry_across_pages:
            current = None
        previous_page = row.row.page
        if row.kind in {"date_heading", "flow"} and row.dates:
            current = row
        segment = by_position.get(row.position)
        if segment is None:
            continue
        source = row if mode == DateMode.PER_TRANSACTION else current
        if mode == DateMode.GROUPED and row.dates:
            raise OperatorFailure("date_attribution", "A movement has competing grouped and individual date structure.")
        if source is None or not source.dates:
            raise OperatorFailure("date_attribution", "A transaction has no supported full-year date context.")
        if len(source.dates) != 1:
            raise AmbiguousStatementError("Multiple material date attributions survive for a transaction segment.")
        output.append(DatedSegment(segment, source.dates[0], source, source.position))
    return tuple(output)
