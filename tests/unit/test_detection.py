import pytest

from pdf_to_ofx.banks.detector import detect_parser
from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.domain.errors import UnsupportedLayoutError
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage


def test_strong_synthetic_markers_are_detected(synthetic_document: ExtractedDocument) -> None:
    assert isinstance(detect_parser(synthetic_document), SyntheticParser)


@pytest.mark.parametrize("text", ["UNKNOWN BANK", "BANCO SINTETICO", "", "Mention: BANCO SINTETICO"])
def test_unknown_or_incomplete_markers_fail(text: str) -> None:
    with pytest.raises(UnsupportedLayoutError):
        detect_parser(ExtractedDocument((ExtractedPage(1, text),)))
