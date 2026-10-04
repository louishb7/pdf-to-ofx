from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from pdf_to_ofx.application.convert import convert_pdf
from pdf_to_ofx.cli import main
from pdf_to_ofx.domain.errors import StatementParseError, StatementValidationError


def test_public_inter_pdf_end_to_end(inter_pdf: Path) -> None:
    result = convert_pdf(inter_pdf)
    assert result.bank_name == "Banco Inter"
    assert len(result.statement.transactions) == 5
    assert result.statement.opening_balance is None
    assert result.statement.closing_balance == Decimal("1000.00")
    assert result.statement.transactions[2].posting_date.isoformat() == "2027-02-02"
    root = ET.fromstring(result.ofx.split("\n\n", 1)[1])
    assert len(root.findall(".//STMTTRN")) == 5
    assert [node.text for node in root.findall(".//TRNAMT")] == ["250.00", "-100.00", "50.00", "-70.00", "20.00"]
    assert root.findtext(".//ACCTID") == "99999999"
    assert result.ofx == convert_pdf(inter_pdf).ofx


def test_public_fixture_reproduces_identical_bytes(inter_pdf: Path, inter_pages: tuple[str, ...],
                                                 write_inter_pdf: Callable, tmp_path: Path) -> None:
    generated = tmp_path / "inter.pdf"
    write_inter_pdf(inter_pages, generated)
    assert generated.read_bytes() == inter_pdf.read_bytes()


@pytest.mark.parametrize(("old", "new", "error"), [
    ("-R$ 100,00", "-R$ BAD", StatementParseError),
    ("R$ 50,00", "R$ 50,01", StatementValidationError),
    ("Saldo do dia: R$ 980,00", "Saldo do dia: R$ 981,00", StatementValidationError),
    ("R$ 1.000,00 R$ 1.000,00 R$ 0,00", "R$ 1.000,01 R$ 1.000,00 R$ 0,00", StatementValidationError),
])
def test_invalid_public_pdf_never_exports(inter_pages: tuple[str, ...], write_inter_pdf: Callable,
                                         tmp_path: Path, old: str, new: str, error: type[Exception]) -> None:
    source = tmp_path / "invalid.pdf"
    output = tmp_path / "invalid.ofx"
    write_inter_pdf(tuple(page.replace(old, new) for page in inter_pages), source)
    with patch("pdf_to_ofx.application.convert.generate_ofx") as exporter:
        with pytest.raises(error):
            convert_pdf(source)
        assert main([str(source), "-o", str(output)]) == 1
        exporter.assert_not_called()
    assert not output.exists()


def test_cli_inter_summary_hides_financial_content(inter_pdf: Path, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    output = tmp_path / "inter.ofx"
    assert main([str(inter_pdf), "-o", str(output)]) == 0
    messages = capsys.readouterr()
    assert messages.err == ""
    assert "Banco Inter" in messages.out
    assert "Transactions: 5" in messages.out
    assert "opening balance unavailable" in messages.out
    for private_shape in ("99999999", "99999", "1000.00", "250.00", "FICTÍCIO"):
        assert private_shape not in messages.out
    assert output.read_text(encoding="ascii") == convert_pdf(inter_pdf).ofx


def test_real_layout_pipeline_has_no_network_calls(inter_pdf: Path) -> None:
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
        assert len(convert_pdf(inter_pdf).statement.transactions) == 5
