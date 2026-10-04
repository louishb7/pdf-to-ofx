"""A successful interpretation stays reviewable independently of export."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import ExportStatus, StatementAnalysis, analyze_pdf
from pdf_to_ofx.domain.errors import OFXGenerationError
from pdf_to_ofx.domain.evidence import AnalysisStatus


@pytest.fixture
def structural_pdf(write_pdf, tmp_path):
    source = tmp_path / "structural.pdf"
    write_pdf("Período: 01/03/2027 a 03/03/2027\n"
              "Saldo inicial: R$ 100,00\nSaldo final: R$ 100,01\n"
              "01/03/2027 CRÉDITO +R$ 0,02 R$ 100,02\n"
              "02/03/2027 PAGAMENTO JOÃO -R$ 0,01 R$ 100,01", source)
    return source


def test_success_without_metadata_remains_visible_after_previous_exportable_result(window, synthetic_pdf, structural_pdf):
    window.load_pdf(synthetic_pdf)
    old_ofx = window.conversion_result.ofx
    window.load_pdf(structural_pdf)
    assert window.analysis == analyze_pdf(structural_pdf)
    assert window.analysis.status == AnalysisStatus.SUCCESS
    assert window.export_readiness.status == ExportStatus.EXPORT_METADATA_REQUIRED
    assert window.selected_file == structural_pdf
    assert window.table.rowCount() == 2
    assert window.table.item(1, 1).text() == "PAGAMENTO JOÃO"
    assert window.summary_labels["count"].text() == "2"
    assert "reconciliados" in window.summary_labels["validation"].text()
    assert "Extrato interpretado" in window.status_label.text()
    assert "dados bancários adicionais" in window.status_label.text()
    assert not window.save_button.isEnabled()
    assert window.conversion_result is None
    assert old_ofx not in repr(window.analysis)
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog, \
         patch("pdf_to_ofx.ui.main_window.write_ofx") as writer:
        window.save_ofx()
    dialog.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize("status,expected", [
    (AnalysisStatus.AMBIGUOUS, "interpretação única"),
    (AnalysisStatus.UNSUPPORTED, "formato ainda não é suportado"),
    (AnalysisStatus.INVALID, "inconsistência"),
])
def test_failed_analysis_has_distinct_status_and_clears_all_previous_export_state(window, synthetic_pdf, structural_pdf, status, expected):
    window.load_pdf(synthetic_pdf)
    failure = StatementAnalysis(status, reason="SENSITIVE TEST TEXT")
    with patch("pdf_to_ofx.ui.main_window.analyze_pdf", return_value=failure):
        window.load_pdf(structural_pdf)
    assert window.analysis == failure
    assert window.export_readiness is None
    assert window.conversion_result is None
    assert window.table.rowCount() == 0
    assert all(label.text() == "—" for label in window.summary_labels.values())
    assert expected in window.status_label.text()
    assert "SENSITIVE" not in window.status_label.text()
    assert not window.save_button.isEnabled()
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog:
        window.save_ofx()
    dialog.assert_not_called()


def test_metadata_missing_then_invalid_selection_clears_parsed_result(window, structural_pdf):
    window.load_pdf(structural_pdf)
    assert window.table.rowCount() == 2
    window.load_pdf(structural_pdf.with_suffix(".txt"))
    assert window.analysis is None
    assert window.export_readiness is None
    assert window.table.rowCount() == 0
    assert window.conversion_result is None
    assert not window.save_button.isEnabled()


def test_success_without_closing_keeps_table_and_explains_export_requirements(window, inter_pdf):
    analysis = analyze_pdf(inter_pdf)
    analysis = replace(analysis, statement=replace(analysis.statement, closing_balance=None))
    with patch("pdf_to_ofx.ui.main_window.analyze_pdf", return_value=analysis):
        window.load_pdf(inter_pdf)
    assert window.table.rowCount() == 5
    assert window.summary_labels["closing"].text() == "Não informado pelo extrato"
    assert window.export_readiness.status == ExportStatus.EXPORT_REQUIREMENTS_MISSING
    assert not window.save_button.isEnabled()
    assert "saldo final" in window.status_label.text()


def test_failed_ofx_generation_preserves_approved_review_and_disables_save(window, synthetic_pdf, inter_pdf):
    window.load_pdf(synthetic_pdf)
    with patch("pdf_to_ofx.ui.main_window.export_analysis", side_effect=OFXGenerationError("SENSITIVE TEST TEXT")):
        window.load_pdf(inter_pdf)
    assert window.analysis.status == AnalysisStatus.SUCCESS
    assert window.table.rowCount() == 5
    assert window.conversion_result is None
    assert not window.save_button.isEnabled()
    assert window.export_readiness.status == ExportStatus.EXPORT_REQUIREMENTS_MISSING
    assert "SENSITIVE" not in window.status_label.text()


def test_metadata_missing_review_works_without_network(window, structural_pdf):
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
        window.load_pdf(structural_pdf)
    assert window.table.rowCount() == 2
    assert not window.save_button.isEnabled()
