"""Resolve index-only references on demand, without persisting source contents."""

from pdf_to_ofx.domain.evidence import SourceSpan
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument
from pdf_to_ofx.pdf.provenance import text_lines


def source_tokens(document: ExtractedDocument, source: SourceSpan,
                  tolerances: Tolerances = Tolerances()) -> tuple[str, ...]:
    if source.basis == "text_line":
        rows = {(page, row): tuple(text.split()) for page, row, text in text_lines(document)}
    else:
        counts: dict[int, int] = {}
        rows = {}
        for row in reconstruct_rows(document, tolerances):
            counts[row.page] = counts.get(row.page, 0) + 1
            rows[row.page, counts[row.page]] = tuple(word.text for word in row.words)
    words = rows.get((source.page, source.row))
    if words is None or source.word_end > len(words):
        raise ValueError("Source interval does not exist in the extracted document.")
    return words[source.word_start:source.word_end]
