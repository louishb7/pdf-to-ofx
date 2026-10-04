"""Reconstruct visual rows without assuming a PDF table or bank layout."""

from dataclasses import dataclass
from math import isfinite

from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.pdf.document import ExtractedDocument, Word


@dataclass(frozen=True, slots=True)
class Tolerances:
    row_y: float = 3.0
    token_gap: float = 12.0
    summary_y: float = 30.0

    def __post_init__(self) -> None:
        for value in (self.row_y, self.token_gap, self.summary_y):
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise ValueError("Structural tolerances must be finite nonnegative distances.")


@dataclass(frozen=True, slots=True)
class Row:
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def page(self) -> int:
        return self.words[0].page


def reconstruct_rows(document: ExtractedDocument, tolerances: Tolerances = Tolerances()) -> tuple[Row, ...]:
    rows: list[Row] = []
    previous_page = 0
    for page in document.pages:
        if page.number <= previous_page or not page.words:
            raise StatementParseError("Positioned words on ordered pages are required.")
        previous_page = page.number
        for word in page.words:
            if (word.page != page.number or not isinstance(word.text, str) or not word.text.strip()
                    or any(type(v) not in (int, float) or not isfinite(v)
                           for v in (word.x0, word.x1, word.top, word.bottom))
                    or word.x1 <= word.x0 or word.bottom <= word.top):
                raise StatementParseError("Invalid positioned word geometry.")
        ordered = sorted(page.words, key=lambda w: ((w.top + w.bottom) / 2, w.x0, w.text))
        group: list[Word] = []
        anchor = 0.0
        for word in ordered:
            center = (word.top + word.bottom) / 2
            # Fixed row anchor prevents transitive merging of adjacent rows.
            if group and center - anchor > tolerances.row_y:
                rows.append(Row(tuple(sorted(group, key=lambda w: w.x0))))
                group = []
            if not group:
                anchor = center
            group.append(word)
        if group:
            rows.append(Row(tuple(sorted(group, key=lambda w: w.x0))))
    return tuple(rows)
