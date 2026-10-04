"""A recognized unknown-bank structure must still fail closed without metadata."""

from unittest.mock import patch

from pdf_to_ofx.application.convert import parse_pdf


def test_generic_statement_without_account_cannot_export_old_gui_result(
    window, synthetic_pdf, write_pdf, tmp_path,
):
    document = tmp_path / "structural.pdf"
    write_pdf(
        "EXTRATO FICTÍCIO ESTRUTURAL\n"
        "Período: 01/03/2027 a 03/03/2027\n"
        "Saldo inicial: R$ 100,00\nSaldo final: R$ 100,01\n"
        "01/03/2027 ALFA R$ 0,02 R$ 100,02\n"
        "02/03/2027 BETA -R$ 0,01 R$ 100,01", document,
    )
    assert len(parse_pdf(document).statement.transactions) == 2
    window.load_pdf(synthetic_pdf)
    window.load_pdf(document)
    assert window.conversion_result is None
    assert not window.save_button.isEnabled()
    assert window.table.rowCount() == 2
    assert window.analysis is not None
    assert window.summary_labels["count"].text() == "2"
    assert "dados bancários" in window.status_label.text()
    with patch("pdf_to_ofx.ui.main_window.QFileDialog.getSaveFileName") as dialog:
        window.save_ofx()
    dialog.assert_not_called()
    assert not document.with_suffix(".ofx").exists()
