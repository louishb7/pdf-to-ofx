"""Widget behavior over public fixtures; no pixels or private financial inputs."""

from collections.abc import Callable
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from pdf_to_ofx.application.convert import convert_pdf
from pdf_to_ofx.domain.errors import (
    ConversionError, OFXGenerationError, PDFExtractionError, StatementParseError,
    StatementValidationError, UnsupportedLayoutError,
)
from pdf_to_ofx.ui import application
from pdf_to_ofx.ui.main_window import MainWindow, format_money


def test_window_starts_with_no_exportable_result(window: MainWindow) -> None:
    assert window.isVisible()
    assert window.windowTitle() == "PDF para OFX"
    assert window.selected_file is None
    assert window.conversion_result is None
    assert window.select_button.isEnabled()
    assert not window.save_button.isEnabled()
    assert window.table.rowCount() == 0
    assert all(label.text() == "—" for label in window.summary_labels.values())
    assert "Selecione um PDF" in window.status_label.text()
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog:
        window.save_ofx()
        dialog.assert_not_called()


@pytest.mark.parametrize(("fixture", "expected"), [
    ("synthetic_pdf", {
        "bank": "Synthetic Bank / synthetic-v1", "period": "01/09/2026 a 05/09/2026",
        "count": "4", "credits": "2", "debits": "2", "opening": "R$ 1.000,00",
        "closing": "R$ 1.365,00", "validation": "Aprovada",
    }),
    ("inter_pdf", {
        "bank": "Banco Inter / inter-digital-v1", "period": "01/02/2027 a 04/02/2027",
        "count": "5", "credits": "3", "debits": "2", "opening": "Não informado pelo extrato",
        "closing": "R$ 1.000,00", "validation": "Aprovada",
    }),
])
def test_public_conversion_summary_and_read_only_table(
    window: MainWindow, request: pytest.FixtureRequest, fixture: str, expected: dict,
) -> None:
    source = request.getfixturevalue(fixture)
    result = convert_pdf(source)
    window.load_pdf(source)
    assert window.selected_file == source
    assert window.conversion_result == result
    assert window.save_button.isEnabled()
    assert {key: label.text() for key, label in window.summary_labels.items()} == expected
    assert window.table.rowCount() == len(result.statement.transactions)
    for row, transaction in enumerate(result.statement.transactions):
        assert window.table.item(row, 0).text() == transaction.posting_date.strftime("%d/%m/%Y")
        assert window.table.item(row, 1).text() == transaction.description
        assert window.table.item(row, 2).text() == ("Crédito" if transaction.amount > 0 else "Débito")
        assert window.table.item(row, 3).text() == format_money(transaction.amount)
        assert window.table.item(row, 4).text() == format_money(transaction.balance_after)
        for column in range(5):
            assert not window.table.item(row, column).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window.table.isSortingEnabled()


def test_select_button_uses_pdf_dialog(window: MainWindow, synthetic_pdf: Path) -> None:
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(synthetic_pdf), "")) as dialog:
        window.select_button.click()
    assert "*.pdf" in dialog.call_args.args[3]
    assert window.conversion_result == convert_pdf(synthetic_pdf)


def test_cancelled_selection_preserves_current_result(window: MainWindow, synthetic_pdf: Path) -> None:
    window.load_pdf(synthetic_pdf)
    result = window.conversion_result
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getOpenFileName", return_value=("", "")), \
         patch("pdf_to_ofx.ui.main_window.convert_pdf") as converter:
        window.select_button.click()
    converter.assert_not_called()
    assert window.conversion_result is result
    assert window.save_button.isEnabled()


def test_conversion_and_cancelled_save_do_not_persist_data(
    window: MainWindow, synthetic_pdf: Path, tmp_path: Path,
) -> None:
    source = tmp_path / "extrato.pdf"
    source.write_bytes(synthetic_pdf.read_bytes())
    window.load_pdf(source)
    status = window.status_label.text()
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=("", "")), \
         patch("pdf_to_ofx.ui.main_window.write_ofx") as writer:
        window.save_button.click()
    writer.assert_not_called()
    assert list(tmp_path.iterdir()) == [source]
    assert window.status_label.text() == status
    assert window.save_button.isEnabled()


@pytest.mark.parametrize("suffix", [".ofx", "", ".OFX"])
def test_save_publishes_exact_core_bytes_with_suggested_name(
    window: MainWindow, inter_pdf: Path, tmp_path: Path, suffix: str,
) -> None:
    window.load_pdf(inter_pdf)
    destination = tmp_path / ("chosen" + suffix)
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(destination), "")) as dialog:
        window.save_button.click()
    assert dialog.call_args.args[2] == str(inter_pdf.with_suffix(".ofx"))
    output = destination if suffix else destination.with_suffix(".ofx")
    assert output.read_bytes() == convert_pdf(inter_pdf).ofx.encode("ascii")
    assert list(tmp_path.iterdir()) == [output]  # Publication leaves no temporary files.
    assert window.status_label.text() == "OFX salvo com sucesso."


def test_existing_destination_is_preserved(window: MainWindow, inter_pdf: Path, tmp_path: Path) -> None:
    window.load_pdf(inter_pdf)
    output = tmp_path / "existing.ofx"
    output.write_bytes(b"previous file")
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(output), "")):
        window.save_ofx()
    assert output.read_bytes() == b"previous file"
    assert list(tmp_path.iterdir()) == [output]
    assert "já existe" in window.status_label.text()
    assert window.save_button.isEnabled()  # A valid result can be saved under another name.


def test_save_failure_is_readable_and_retryable(window: MainWindow, synthetic_pdf: Path, tmp_path: Path) -> None:
    window.load_pdf(synthetic_pdf)
    output = tmp_path / "absent-directory" / "output.ofx"
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(output), "")):
        window.save_ofx()
    assert not output.exists()
    assert "Não foi possível salvar" in window.status_label.text()
    assert window.save_button.isEnabled()


def test_wrong_output_extension_does_not_write(window: MainWindow, synthetic_pdf: Path, tmp_path: Path) -> None:
    window.load_pdf(synthetic_pdf)
    output = tmp_path / "wrong.pdf"
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(output), "")), \
         patch("pdf_to_ofx.ui.main_window.write_ofx") as writer:
        window.save_ofx()
    writer.assert_not_called()
    assert not output.exists()
    assert "extensão .ofx" in window.status_label.text()


def test_new_selection_replaces_all_previous_fields(window: MainWindow, synthetic_pdf: Path, inter_pdf: Path) -> None:
    window.load_pdf(synthetic_pdf)
    window.load_pdf(inter_pdf)
    assert window.conversion_result == convert_pdf(inter_pdf)
    assert window.selected_file == inter_pdf
    assert window.summary_labels["bank"].text() == "Banco Inter / inter-digital-v1"
    assert window.summary_labels["opening"].text() == "Não informado pelo extrato"
    assert window.table.rowCount() == 5
    assert window.table.item(0, 1).text() == "CRÉDITO FICTÍCIO ALFA"


def test_old_state_is_cleared_before_pipeline_runs(window: MainWindow, synthetic_pdf: Path, inter_pdf: Path) -> None:
    window.load_pdf(synthetic_pdf)
    new_result = convert_pdf(inter_pdf)

    def convert_after_reset(path: Path):
        assert path == inter_pdf
        assert window.selected_file is None and window.conversion_result is None
        assert window.table.rowCount() == 0
        assert all(label.text() == "—" for label in window.summary_labels.values())
        assert not window.save_button.isEnabled() and not window.select_button.isEnabled()
        with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog:
            window.save_ofx()
        dialog.assert_not_called()
        return new_result

    with patch("pdf_to_ofx.ui.main_window.convert_pdf", side_effect=convert_after_reset):
        window.load_pdf(inter_pdf)
    assert window.conversion_result == new_result
    assert window.save_button.isEnabled() and window.select_button.isEnabled()


def test_failed_publication_cleans_temporary_ofx_and_keeps_result_for_retry(
    window: MainWindow, inter_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    window.load_pdf(inter_pdf)
    result = window.conversion_result
    output = tmp_path / "failed.ofx"
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(output), "")), \
         patch("pdf_to_ofx.application.export.Path.hardlink_to", side_effect=PermissionError("SENSITIVE TEST TEXT")):
        window.save_ofx()
    assert list(tmp_path.iterdir()) == []
    assert window.conversion_result is result and window.save_button.isEnabled()
    assert "Não foi possível salvar" in window.status_label.text()
    assert "SENSITIVE TEST TEXT" not in window.status_label.text()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(("error_type", "expected"), [
    (PDFExtractionError, "texto selecionável"),
    (UnsupportedLayoutError, "não é suportado"),
    (StatementParseError, "incompleto"),
    (StatementValidationError, "não passaram na validação"),
    (OFXGenerationError, "dados bancários"),
    (ConversionError, "com segurança"),
    (RuntimeError, "com segurança"),
])
def test_failure_clears_old_result_and_never_exposes_exception_text(
    window: MainWindow, synthetic_pdf: Path, inter_pdf: Path,
    error_type: type[Exception], expected: str, capsys: pytest.CaptureFixture,
) -> None:
    window.load_pdf(synthetic_pdf)
    with patch("pdf_to_ofx.ui.main_window.convert_pdf", side_effect=error_type("SENSITIVE TEST TEXT")):
        window.load_pdf(inter_pdf)
    assert window.conversion_result is None
    assert window.selected_file is None
    assert window.table.rowCount() == 0
    assert all(label.text() == "—" for label in window.summary_labels.values())
    assert not window.save_button.isEnabled()
    assert window.select_button.isEnabled()
    assert expected in window.status_label.text()
    assert "SENSITIVE TEST TEXT" not in window.status_label.text()
    assert window.file_label.text() == "Nenhum PDF selecionado."
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog, \
         patch("pdf_to_ofx.ui.main_window.write_ofx") as writer:
        window.save_button.click()
        window.save_ofx()  # The action itself must reject stale export, too.
    dialog.assert_not_called()
    writer.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("kind", ["missing", "wrong_extension", "directory"])
def test_invalid_file_clears_previous_result_before_core_is_called(
    window: MainWindow, synthetic_pdf: Path, tmp_path: Path, kind: str,
) -> None:
    window.load_pdf(synthetic_pdf)
    path = tmp_path / "missing.pdf"
    if kind == "wrong_extension":
        path = tmp_path / "wrong.txt"
        path.write_text("FICTITIOUS")
    elif kind == "directory":
        path = tmp_path / "directory.pdf"
        path.mkdir()
    with patch("pdf_to_ofx.ui.main_window.convert_pdf") as converter:
        window.load_pdf(path)
    converter.assert_not_called()
    assert window.conversion_result is None
    assert window.table.rowCount() == 0
    assert not window.save_button.isEnabled()
    assert "arquivo existente" in window.status_label.text()


def test_real_financial_validation_failure_blocks_old_export(
    window: MainWindow, synthetic_pdf: Path, synthetic_text: str, write_pdf: Callable, tmp_path: Path,
) -> None:
    window.load_pdf(synthetic_pdf)
    invalid = tmp_path / "bad-balance.pdf"
    write_pdf(synthetic_text.replace("1.365,00 C", "1.365,01 C"), invalid)
    window.load_pdf(invalid)
    assert "não passaram na validação" in window.status_label.text()
    assert window.conversion_result is None
    assert not window.save_button.isEnabled()
    assert window.table.rowCount() == 0
    assert not invalid.with_suffix(".ofx").exists()


def test_textless_pdf_reports_readable_error(window: MainWindow, tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    source = tmp_path / "no-text.pdf"
    canvas = Canvas(str(source), invariant=1)
    canvas.rect(30, 30, 100, 100, fill=1)
    canvas.save()
    window.load_pdf(source)
    assert "PDFs digitalizados não são suportados" in window.status_label.text()
    assert window.conversion_result is None
    assert not window.save_button.isEnabled()


def test_single_local_pdf_drop_preserves_source(window: MainWindow, synthetic_pdf: Path, tmp_path: Path) -> None:
    source = tmp_path / "EXTRATO.PDF"
    original = synthetic_pdf.read_bytes()
    source.write_bytes(original)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                            mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dragEnterEvent(enter)
    assert enter.isAccepted() and enter.dropAction() == Qt.DropAction.CopyAction
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                      mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(drop)
    assert drop.isAccepted() and drop.dropAction() == Qt.DropAction.CopyAction
    assert window.selected_file == source
    assert window.table.rowCount() == 4
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("urls", [
    [], [QUrl("https://example.invalid/statement.pdf")],
    [QUrl.fromLocalFile("/tmp/not-pdf.txt")],
    [QUrl.fromLocalFile("/tmp/first.pdf"), QUrl.fromLocalFile("/tmp/second.pdf")],
])
def test_remote_multiple_and_non_pdf_drops_are_ignored(window: MainWindow, urls: list[QUrl]) -> None:
    mime = QMimeData()
    mime.setUrls(urls)
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction,
                            mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction,
                      mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    with patch("pdf_to_ofx.ui.main_window.convert_pdf") as converter:
        window.dragEnterEvent(enter)
        window.dropEvent(drop)
    assert not enter.isAccepted() and not drop.isAccepted()
    converter.assert_not_called()
    assert window.conversion_result is None


def test_gui_selection_conversion_and_save_work_without_network(
    window: MainWindow, inter_pdf: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "offline.ofx"
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network forbidden")), \
         patch("pdf_to_ofx.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(inter_pdf), "")), \
         patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(output), "")):
        window.select_button.click()
        assert window.table.rowCount() == 5
        window.save_button.click()
        assert output.read_bytes() == convert_pdf(inter_pdf).ofx.encode("ascii")


@pytest.mark.parametrize(("value", "expected"), [
    (None, "Não informado pelo extrato"),
    (Decimal("0.01"), "R$ 0,01"), (Decimal("-0.01"), "R$ -0,01"),
    (Decimal("-0.00"), "R$ 0,00"),
    (Decimal("1234567890.12"), "R$ 1.234.567.890,12"),
])
def test_brazilian_display_uses_decimal_without_rounding_context(value: Decimal | None, expected: str) -> None:
    with localcontext() as context:
        context.prec = 2
        assert format_money(value) == expected


def test_desktop_entry_point_shows_window_and_returns_event_loop_status(
    qapp: QApplication, window: MainWindow,
) -> None:
    with patch.object(application, "MainWindow", return_value=window), \
         patch.object(QApplication, "exec", return_value=0) as event_loop:
        assert application.main([]) == 0
    assert window.isVisible()
    event_loop.assert_called_once()
