"""Conservative page frames, independent of amount and date modes."""

from itertools import groupby
import re

from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.generic.semantics import has_financial_signal
from pdf_to_ofx.generic.structure import Row
from pdf_to_ofx.generic.operators.candidates import OperatorFailure, _has_date, _period, _stamp, leading_date


def contact_footer_rows(rows: tuple[Row, ...]) -> int:
    """Recognize contact roles, never arbitrary repeated descriptions."""
    counts = []
    for _, grouped in groupby(rows, key=lambda row: row.page):
        page = list(grouped)
        last = page[-1]
        labels = set(re.findall(r"\b(?:sac|ouvidoria|telefone|atendimento)\b", last.text.casefold()))
        count = 0
        if len(labels) >= 2 and not has_financial_signal(last.text) and leading_date(last) is None:
            count = 1
            if len(page) >= 2:
                previous = page[-2]
                if (re.match(r"^(?:fale|contato|atendimento)\b", previous.text, re.IGNORECASE)
                        and not has_financial_signal(previous.text) and leading_date(previous) is None):
                    count = 2
        counts.append(count)
    return counts[0] if counts and len(set(counts)) == 1 else 0


def continue_pages(rows: tuple[Row, ...], profile: LayoutProfile) -> tuple[Row, ...]:
    if not rows:
        raise StatementParseError("Positioned statement rows are required.")
    if profile.trailing_note_rows and profile.transaction_left is None:
        raise StatementParseError("Trailing notes require an explicit transaction boundary.")
    pages = [list(group) for _, group in groupby(rows, key=lambda r: r.page)]
    reference = [r.text for r in pages[0][:profile.repeated_header_rows]]
    if any((has_financial_signal(row.text) or leading_date(row)) and not _period(row)
           for row in pages[0][:profile.repeated_header_rows]):
        raise StatementParseError("Repeated identity headers cannot contain monetary content.")
    output = []
    for index, page in enumerate(pages):
        if profile.footer_rows:
            if len(page) <= profile.footer_rows:
                raise StatementParseError("Footer consumes a complete page.")
            for row in page[-profile.footer_rows:]:
                if has_financial_signal(row.text) or (_has_date(row.text) and not _stamp(row)):
                    raise StatementParseError("Financial content cannot be removed as a footer.")
                pagination = re.search(r"([0-9]+)\s*(?:de|/)\s*([0-9]+)$", row.text) if _stamp(row) else None
                if pagination and (int(pagination[1]), int(pagination[2])) != (index + 1, len(pages)):
                    raise OperatorFailure("pagination_consistency", "Declared pagination differs from the extracted pages.")
            page = page[:-profile.footer_rows]
        if index and profile.repeated_header_rows:
            if [r.text for r in page[:profile.repeated_header_rows]] != reference:
                raise StatementParseError("Repeated page headers differ from the initial declaration.")
            page = page[profile.repeated_header_rows:]
        if index == len(pages) - 1 and profile.trailing_note_rows:
            if len(page) < profile.trailing_note_rows:
                raise StatementParseError("Trailing notes consume a complete page.")
            for row in page[-profile.trailing_note_rows:]:
                if (has_financial_signal(row.text) or _has_date(row.text)
                        or row.words[0].x0 >= profile.transaction_left - profile.tolerances.row_y):
                    raise StatementParseError("Transaction-column or financial content cannot be removed as notes.")
            page = page[:-profile.trailing_note_rows]
        output.extend(page)
    return tuple(output)
