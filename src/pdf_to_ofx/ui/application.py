"""Desktop entry point, separate from the developer CLI."""

import sys

from PySide6.QtWidgets import QApplication

from pdf_to_ofx.ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("PDF para OFX")
    window = MainWindow()
    window.show()
    return app.exec()
