"""Index nonblank lines for the two historical textual grammars."""

import re

from pdf_to_ofx.domain.evidence import SourceSpan
from pdf_to_ofx.pdf.document import ExtractedDocument


def text_lines(document: ExtractedDocument) -> tuple[tuple[int, int, str], ...]:
    return tuple((page.number, index, line) for page in document.pages
                 for index, line in enumerate((line.strip() for line in page.text.splitlines() if line.strip()), 1))


def text_span(page: int, row: int, text: str, start: int = 0, end: int | None = None) -> SourceSpan:
    words = list(re.finditer(r"\S+", text))
    end = len(text) if end is None else end
    indexes = [i for i, word in enumerate(words) if word.start() < end and word.end() > start]
    return SourceSpan(page, row, indexes[0], indexes[-1] + 1, "text_line")
