"""A small read-only review window over application conversion and publication."""

from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from pdf_to_ofx.application.convert import ConversionResult, convert_pdf
from pdf_to_ofx.application.export import write_ofx
from pdf_to_ofx.domain.errors import (
    OFXGenerationError, PDFExtractionError, StatementParseError,
    StatementValidationError, UnsupportedLayoutError,
)


def format_money(value: Decimal | None) -> str:
    """Brazilian display only; domain values and OFX bytes remain untouched."""
    if value is None:
        return "Não informado pelo extrato"
    formatted = format(value if value != 0 else Decimal("0"), ",.2f")
    return "R$ " + formatted.translate(str.maketrans(",.", ".,"))


def conversion_error_message(error: Exception) -> str:
    # Never interpolate exception text: library failures can contain PDF data.
    if isinstance(error, PDFExtractionError):
        return ("Não foi possível extrair texto do PDF. Use um PDF com texto selecionável "
                "e verifique se o arquivo está danificado ou protegido. PDFs digitalizados "
                "não são suportados.")
    if isinstance(error, UnsupportedLayoutError):
        return "O banco ou o formato deste extrato ainda não é suportado."
    if isinstance(error, StatementParseError):
        return "O extrato está incompleto ou não segue o formato suportado. O OFX não foi gerado."
    if isinstance(error, StatementValidationError):
        return "Os valores ou saldos do extrato não passaram na validação. O OFX não foi gerado."
    if isinstance(error, OFXGenerationError):
        return ("Não foi possível gerar o OFX. Os dados bancários ou identificadores "
                "necessários estão ausentes, inválidos ou inconsistentes.")
    return "Não foi possível processar este PDF com segurança. Verifique o arquivo e tente novamente."


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.selected_file: Path | None = None
        self.conversion_result: ConversionResult | None = None
        self.setWindowTitle("PDF para OFX")
        self.resize(1000, 720)
        self.setMinimumSize(760, 580)
        self.setAcceptDrops(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.setCentralWidget(central)

        title = QLabel("PDF para OFX")
        font = title.font()
        font.setPointSize(font.pointSize() + 6)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        privacy = QLabel("Processamento realizado localmente. Nenhum extrato é enviado pela internet.")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        actions = QHBoxLayout()
        self.select_button = QPushButton("Selecionar PDF")
        self.select_button.clicked.connect(self.select_pdf)
        actions.addWidget(self.select_button)
        actions.addWidget(QLabel("ou arraste um único PDF para esta janela"))
        actions.addStretch()
        self.save_button = QPushButton("Salvar OFX")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_ofx)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        self.file_label = QLabel("Nenhum PDF selecionado.")
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        summary = QGroupBox("Resumo da conversão")
        grid = QGridLayout(summary)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        self.summary_labels: dict[str, QLabel] = {}
        fields = (
            ("bank", "Banco / layout"), ("period", "Período"),
            ("count", "Lançamentos"), ("credits", "Créditos"),
            ("debits", "Débitos"), ("opening", "Saldo inicial"),
            ("closing", "Saldo final"), ("validation", "Validação"),
        )
        for index, (key, caption) in enumerate(fields):
            label = QLabel("—")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            self.summary_labels[key] = label
            row, column = index // 2, (index % 2) * 2
            grid.addWidget(QLabel(caption + ":"), row, column)
            grid.addWidget(label, row, column + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addWidget(summary)

        layout.addWidget(QLabel("Lançamentos · somente leitura"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Data", "Descrição", "Tipo", "Valor", "Saldo após lançamento"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, stretch=1)

        self.status_label = QLabel("Selecione um PDF para começar.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Status da conversão")
        layout.addWidget(self.status_label)
        notice = QLabel("Compatibilidade com Athenas ainda não validada.")
        notice.setWordWrap(True)
        layout.addWidget(notice)

    def _clear_result(self) -> None:
        # Clear memory AND visible fields before every attempt, including invalid
        # paths. A failed second conversion cannot export the previous OFX.
        self.selected_file = None
        self.conversion_result = None
        self.save_button.setEnabled(False)
        self.file_label.setText("Nenhum PDF selecionado.")
        self.table.clearContents()
        self.table.setRowCount(0)
        for label in self.summary_labels.values():
            label.setText("—")

    def _error(self, message: str) -> None:
        self.status_label.setText("Erro: " + message)

    def select_pdf(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Selecionar extrato PDF", "", "Arquivos PDF (*.pdf)")
        if filename:
            self.load_pdf(Path(filename))

    def load_pdf(self, path: Path) -> None:
        self._clear_result()
        self.select_button.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)
        self.status_label.setText("Processando PDF localmente…")
        self.status_label.repaint()
        try:
            if path.suffix.lower() != ".pdf" or not path.is_file():
                self._error("Selecione um arquivo existente com extensão .pdf.")
                return
            result = convert_pdf(path)
            self._show_result(result)
            self.selected_file = path
            self.conversion_result = result
            self.file_label.setText(path.name)
            self.save_button.setEnabled(True)
            self.status_label.setText("Conversão concluída. Revise os lançamentos e escolha Salvar OFX.")
        except Exception as error:
            self._clear_result()
            self._error(conversion_error_message(error))
        finally:
            self.select_button.setEnabled(True)
            self.unsetCursor()

    def _show_result(self, result: ConversionResult) -> None:
        statement = result.statement
        values = {
            "bank": f"{result.bank_name} / {statement.layout_id}",
            "period": f"{statement.period_start:%d/%m/%Y} a {statement.period_end:%d/%m/%Y}",
            "count": str(len(statement.transactions)),
            "credits": str(sum(t.amount > 0 for t in statement.transactions)),
            "debits": str(sum(t.amount < 0 for t in statement.transactions)),
            "opening": format_money(statement.opening_balance),
            "closing": format_money(statement.closing_balance),
            "validation": "Aprovada",
        }
        for key, value in values.items():
            self.summary_labels[key].setText(value)
        self.table.setRowCount(len(statement.transactions))
        for row, transaction in enumerate(statement.transactions):
            cells = (
                transaction.posting_date.strftime("%d/%m/%Y"), transaction.description,
                "Crédito" if transaction.amount >= 0 else "Débito",
                format_money(transaction.amount), format_money(transaction.balance_after),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(text)
                if column >= 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()

    def save_ofx(self) -> None:
        # Guard the action itself, rather than relying only on the button state.
        if self.conversion_result is None or self.selected_file is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Salvar OFX · escolha um nome novo", str(self.selected_file.with_suffix(".ofx")),
            "Arquivos OFX (*.ofx)", options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not filename:
            return
        output = Path(filename)
        if not output.suffix:
            output = output.with_suffix(".ofx")
        if output.suffix.lower() != ".ofx":
            self._error("Escolha um nome de arquivo com extensão .ofx.")
            return
        try:
            write_ofx(output, self.conversion_result.ofx)
        except FileExistsError:
            self._error("O arquivo de destino já existe. Escolha outro nome; nenhum arquivo foi substituído.")
        except Exception:
            self._error("Não foi possível salvar o OFX. Verifique a pasta e as permissões e tente novamente.")
        else:
            self.status_label.setText("OFX salvo com sucesso.")

    @staticmethod
    def _dropped_pdf(data: QMimeData) -> Path | None:
        urls = data.urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() == ".pdf":
                return path
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._dropped_pdf(event.mimeData()) is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        path = self._dropped_pdf(event.mimeData())
        if path is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.load_pdf(path)
