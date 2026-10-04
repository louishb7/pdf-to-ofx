"""The small parser boundary consumed by the application service."""

from typing import Protocol

from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.pdf.document import ExtractedDocument


class BankParser(Protocol):
    bank_name: str
    layout_id: str

    def parse(self, document: ExtractedDocument) -> Statement: ...
