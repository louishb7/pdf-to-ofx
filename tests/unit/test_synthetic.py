from datetime import date
from decimal import Decimal

import pytest

from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.domain.errors import StatementParseError
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage


def test_parses_all_normalized_fields(statement: Statement) -> None:
    assert statement.bank_id == "synthetic"
    assert statement.layout_id == "synthetic-v1"
    assert statement.period_start == date(2026, 9, 1)
    assert statement.period_end == date(2026, 9, 5)
    assert statement.opening_balance == Decimal("1000.00")
    assert statement.closing_balance == Decimal("1365.00")
    assert [transaction.posting_date for transaction in statement.transactions] == [
        date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 5),
    ]
    assert [transaction.description for transaction in statement.transactions] == [
        "PIX RECEBIDO EMPRESA TESTE", "TARIFA BANCARIA",
        "PAGAMENTO FORNECEDOR TESTE", "PIX RECEBIDO CLIENTE DEMO",
    ]
    assert [transaction.amount for transaction in statement.transactions] == list(map(Decimal, [
        "500.00", "-35.00", "-200.00", "100.00",
    ]))


@pytest.mark.parametrize(("old", "new"), [
    ("R$ 35,00 D", "R$ 35,xx D"), ("R$ 35,00 D", "35,00"),
    ("02/09/2026", "31/09/2026"), ("02/09/2026", "2026-09-02"),
    ("Periodo:", "Dates:"), ("Saldo anterior:", "Unknown balance:"),
    ("Saldo final: R$ 1.365,00 C", ""),
    ("TARIFA BANCARIA", ""), ("R$ 35,00 D", "R$ 35,00 D R$ 10,00 C"),
    ("Saldo final:", "UNEXPECTED TEXT\nSaldo final:"),
])
def test_malformed_fields_and_rows_are_never_skipped(synthetic_text: str, old: str, new: str) -> None:
    document = ExtractedDocument((ExtractedPage(1, synthetic_text.replace(old, new)),))
    with pytest.raises(StatementParseError):
        SyntheticParser().parse(document)


def test_pages_are_parsed_in_document_order(synthetic_text: str, statement: Statement) -> None:
    before, after = synthetic_text.split("03/09/2026", maxsplit=1)
    document = ExtractedDocument((ExtractedPage(1, before), ExtractedPage(2, "03/09/2026" + after)))
    assert SyntheticParser().parse(document) == statement


def test_empty_document_is_rejected() -> None:
    with pytest.raises(StatementParseError):
        SyntheticParser().parse(ExtractedDocument(()))
