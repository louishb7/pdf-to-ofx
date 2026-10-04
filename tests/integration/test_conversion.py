from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from pdf_to_ofx.application.convert import convert_pdf
from pdf_to_ofx.cli import main
from pdf_to_ofx.domain.errors import PDFExtractionError, StatementParseError, StatementValidationError, UnsupportedLayoutError
from pdf_to_ofx.pdf.extractor import extract_pdf


def test_actual_synthetic_pdf_to_ofx(synthetic_pdf: Path) -> None:
    result = convert_pdf(synthetic_pdf)
    assert result.bank_name == "Synthetic Bank"
    assert result.statement.opening_balance == Decimal("1000.00")
    assert result.statement.closing_balance == Decimal("1365.00")
    assert len(result.statement.transactions) == 4
    root = ET.fromstring(result.ofx.split("\n\n", maxsplit=1)[1])
    assert [element.text for element in root.findall(".//TRNAMT")] == [
        "500.00", "-35.00", "-200.00", "100.00",
    ]
    assert root.findtext(".//LEDGERBAL/BALAMT") == "1365.00"
    assert result.ofx == convert_pdf(synthetic_pdf).ofx


def test_fixture_regeneration_is_reproducible(synthetic_pdf: Path, synthetic_text: str,
                                              write_pdf: Callable, tmp_path: Path) -> None:
    regenerated = tmp_path / "regenerated.pdf"
    write_pdf(synthetic_text, regenerated)
    assert regenerated.read_bytes() == synthetic_pdf.read_bytes()


def test_invalid_balance_never_reaches_export(synthetic_text: str, write_pdf: Callable,
                                             tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    invalid = tmp_path / "invalid.pdf"
    output = tmp_path / "invalid.ofx"
    write_pdf(synthetic_text.replace("1.365,00 C", "1.365,01 C"), invalid)
    with patch("pdf_to_ofx.application.convert.generate_ofx") as exporter:
        with pytest.raises(StatementValidationError, match="closing balance"):
            convert_pdf(invalid)
        assert main([str(invalid), "-o", str(output)]) == 1
        exporter.assert_not_called()
    assert not output.exists()
    assert "Conversion failed" in capsys.readouterr().err


def test_malformed_transaction_pdf_fails(synthetic_text: str, write_pdf: Callable, tmp_path: Path) -> None:
    invalid = tmp_path / "malformed.pdf"
    write_pdf(synthetic_text.replace("35,00 D", "35,xx D"), invalid)
    with pytest.raises(StatementParseError):
        convert_pdf(invalid)


def test_unknown_pdf_fails_explicitly(write_pdf: Callable, tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.pdf"
    write_pdf("UNSUPPORTED FICTITIOUS BANK\nNO REAL DATA", unknown)
    with pytest.raises(UnsupportedLayoutError):
        convert_pdf(unknown)


def test_image_only_pdf_has_no_ocr_fallback(tmp_path: Path) -> None:
    # A page with a drawn rectangle and no text follows the image-only failure path.
    from reportlab.pdfgen.canvas import Canvas

    scanned = tmp_path / "no_text.pdf"
    canvas = Canvas(str(scanned), invariant=1)
    canvas.rect(30, 30, 200, 200, fill=1)
    canvas.save()
    with pytest.raises(PDFExtractionError, match="no usable text"):
        extract_pdf(scanned)


def test_pdf_without_pages_fails(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    empty = tmp_path / "empty.pdf"
    Canvas(str(empty), invariant=1).save()
    with pytest.raises(PDFExtractionError, match="no pages"):
        extract_pdf(empty)


def test_empty_page_in_mixed_pdf_is_not_silently_ignored(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    mixed = tmp_path / "mixed.pdf"
    canvas = Canvas(str(mixed), invariant=1)
    canvas.drawString(40, 800, "SYNTHETIC TEXT")
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    with pytest.raises(PDFExtractionError, match="page 2"):
        extract_pdf(mixed)


@pytest.mark.parametrize("exists", [False, True])
def test_missing_or_corrupt_pdf_fails_with_chained_cause(tmp_path: Path, exists: bool) -> None:
    invalid = tmp_path / "bad.pdf"
    if exists:
        invalid.write_text("This is not a PDF.", encoding="ascii")
    with pytest.raises(PDFExtractionError) as failure:
        extract_pdf(invalid)
    assert failure.value.__cause__ is not None


def test_cli_exports_summary_without_descriptions(synthetic_pdf: Path, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    output = tmp_path / "statement.ofx"
    assert main([str(synthetic_pdf), "--output", str(output)]) == 0
    messages = capsys.readouterr()
    assert messages.err == ""
    assert "Synthetic Bank" in messages.out
    assert "Transactions: 4 (credits: 2, debits: 2)" in messages.out
    assert "1000.00" in messages.out and "1365.00" in messages.out
    assert "Validation: passed" in messages.out
    assert "EMPRESA TESTE" not in messages.out
    assert output.read_text(encoding="ascii") == convert_pdf(synthetic_pdf).ofx


def test_cli_default_output_and_overwrite_protection(synthetic_pdf: Path, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(synthetic_pdf.read_bytes())
    original = source.read_bytes()
    assert main([str(source)]) == 0
    export = source.with_suffix(".ofx").read_bytes()
    assert main([str(source)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert source.with_suffix(".ofx").read_bytes() == export
    assert main([str(source), "-o", str(source)]) == 1
    assert source.read_bytes() == original


def test_cli_output_io_errors_are_readable(synthetic_pdf: Path, tmp_path: Path,
                                         capsys: pytest.CaptureFixture) -> None:
    output = tmp_path / "nonexistent-directory" / "output.ofx"
    assert main([str(synthetic_pdf), "-o", str(output)]) == 1
    assert "Unable to write" in capsys.readouterr().err


def test_cli_directory_input_fails_without_traceback(capsys: pytest.CaptureFixture) -> None:
    assert main(["."]) == 1
    assert "Input must name a local PDF file" in capsys.readouterr().err


def test_failed_publish_leaves_no_partial_output(synthetic_pdf: Path, tmp_path: Path) -> None:
    output = tmp_path / "output.ofx"
    with patch("pdf_to_ofx.cli.Path.hardlink_to", side_effect=OSError("Simulated write failure")):
        assert main([str(synthetic_pdf), "-o", str(output)]) == 1
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_output_symlink(synthetic_pdf: Path, tmp_path: Path) -> None:
    target = tmp_path / "existing.ofx"
    target.write_text("existing export", encoding="ascii")
    output = tmp_path / "link.ofx"
    output.symlink_to(target)
    assert main([str(synthetic_pdf), "-o", str(output)]) == 1
    assert target.read_text(encoding="ascii") == "existing export"


def test_conversion_works_with_network_disabled(synthetic_pdf: Path) -> None:
    with patch("socket.socket", side_effect=AssertionError("Network access forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        assert len(convert_pdf(synthetic_pdf).statement.transactions) == 4
