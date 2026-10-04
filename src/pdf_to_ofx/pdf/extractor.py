"""Extract digital text only, without OCR or external services."""

from pathlib import Path

import pdfplumber

from pdf_to_ofx.domain.errors import PDFExtractionError
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage


def extract_pdf(path: Path) -> ExtractedDocument:
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise PDFExtractionError("PDF contains no pages.")
            pages = []
            for number, page in enumerate(pdf.pages, start=1):
                text = page.extract_text()
                if not text or not text.strip():
                    raise PDFExtractionError(
                        f"PDF page {number} has no usable text; scanned PDFs are unsupported."
                    )
                pages.append(ExtractedPage(number, text))
            return ExtractedDocument(tuple(pages))
    except PDFExtractionError:
        raise
    except Exception as exc:
        # pdfplumber/pdfminer expose varied failures at this third-party boundary.
        # Preserve the cause, while keeping PDF contents out of public messages.
        raise PDFExtractionError("Unable to extract text from the local PDF.") from exc
