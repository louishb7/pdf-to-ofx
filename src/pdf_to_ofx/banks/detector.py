"""Recognize only explicit supported layout markers; never guess."""

from pdf_to_ofx.banks.base import BankParser
from pdf_to_ofx.banks.inter import InterParser, matches_layout
from pdf_to_ofx.banks.synthetic import HEADERS, SyntheticParser, document_lines
from pdf_to_ofx.domain.errors import UnsupportedLayoutError
from pdf_to_ofx.pdf.document import ExtractedDocument


def detect_parser(document: ExtractedDocument) -> BankParser:
    synthetic = document_lines(document)[:2] == HEADERS
    inter = matches_layout(document)
    if synthetic:
        return SyntheticParser()
    if inter:
        return InterParser()
    raise UnsupportedLayoutError("Unsupported bank or statement layout.")
