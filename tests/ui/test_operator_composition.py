"""The GUI reviews composed statements without OFX identity or UI parsing."""

from unittest.mock import patch

from pdf_to_ofx.application.convert import ExportStatus
from pdf_to_ofx.domain.evidence import AnalysisStatus


def test_operator_dates_inside_rows_are_visible_without_export_metadata(window, write_pdf, tmp_path):
    path = tmp_path / "fictitious.pdf"
    write_pdf("Período: 01/03/2027 a 03/03/2027\nSaldo inicial: 0,00\nSaldo final: 1,00\n"
              "OPERAÇÃO 01/03/2027 FICTÍCIA +1,00", path)
    with patch("pdf_to_ofx.generic.parser.GenericStatementParser.interpret", side_effect=AssertionError("Legacy grammar")):
        window.load_pdf(path)
    assert window.analysis.status == AnalysisStatus.SUCCESS
    assert window.analysis.financial_scope.region_id == "main"
    assert window.table.rowCount() == 1
    assert window.table.item(0, 1).text() == "OPERAÇÃO FICTÍCIA"
    assert window.export_readiness.status == ExportStatus.EXPORT_METADATA_REQUIRED
    assert not window.save_button.isEnabled()
    assert window.conversion_result is None
