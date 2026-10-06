"""Local acceptance through the existing GUI, with no PDF contents in output.

Run with QT_QPA_PLATFORM=offscreen for headless verification. No files are
written. Financial summary fields require an explicit flag.
"""

import argparse
import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from pdf_to_ofx.application.convert import analyze_pdf, assess_export_readiness, ExportStatus
from pdf_to_ofx.domain.evidence import AnalysisStatus
from pdf_to_ofx.generic.semantics import parse_date, parse_money
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens
from pdf_to_ofx.ui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--show-financial-summary", action="store_true")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    complete = True
    for index, path in enumerate(args.pdfs, 1):
        analysis = analyze_pdf(path)
        statement = analysis.statement
        readiness = assess_export_readiness(analysis)
        window = MainWindow()
        window.load_pdf(path)
        sampled = []
        if statement:
            document = extract_pdf(path)
            sampled = sorted({0, len(statement.transactions) // 2, len(statement.transactions) - 1})
            for position in sampled:
                transaction = statement.transactions[position]
                source = analysis.provenance.transactions[position]
                date_text = " ".join(source_tokens(document, source.date_source))
                amount_text = " ".join(source_tokens(document, source.amount_source))
                direction_text = " ".join(source_tokens(document, source.direction_source))
                description = " ".join(token for span in source.description_sources for token in source_tokens(document, span))
                amount, direction = parse_money(amount_text), parse_money(direction_text)
                assert parse_date(date_text) == transaction.posting_date, "Sample date differs from PDF."
                assert amount is not None and abs(amount.amount) == abs(transaction.amount), "Sample amount differs from PDF."
                assert direction is not None and direction.amount.is_signed() == transaction.amount.is_signed(), "Sample direction differs from PDF."
                assert description == transaction.description, "Sample description differs from PDF."
        assert window.table.rowCount() == (len(statement.transactions) if statement else 0)
        ready = readiness.status == ExportStatus.READY_TO_EXPORT
        assert (window.conversion_result is not None) == ready
        assert window.save_button.isEnabled() == ready
        report = {
            "input_index": index, "status": analysis.status.value,
            "transactions": len(statement.transactions) if statement else None,
            "opening_available": statement.opening_balance is not None if statement else None,
            "reconciliation": analysis.evidence.reconciliation_level,
            "currency": str(statement.currency) if statement and statement.currency else None,
            "readiness": readiness.status.value, "GUI_OFX_generated": ready,
            "PDF_statement_sample_positions": sampled,
        }
        if args.show_financial_summary:
            report.update(period=[str(statement.period_start), str(statement.period_end)] if statement else None,
                          closing_balance=str(statement.closing_balance) if statement else None)
        print(json.dumps(report, ensure_ascii=False))
        complete &= analysis.status == AnalysisStatus.SUCCESS and ready
        window.close()
        window.deleteLater()
        app.processEvents()
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
