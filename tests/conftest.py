"""Shared synthetic inputs, never private bank documents."""

import importlib.util
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.banks.inter import InterParser
from pdf_to_ofx.domain.models import BankAccount, Statement, Transaction
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "synthetic"


@pytest.fixture
def synthetic_pdf() -> Path:
    return FIXTURE_DIRECTORY / "statement.pdf"


@pytest.fixture
def synthetic_text() -> str:
    return (FIXTURE_DIRECTORY / "statement.txt").read_text(encoding="utf-8")


@pytest.fixture
def synthetic_document(synthetic_text: str) -> ExtractedDocument:
    return ExtractedDocument((ExtractedPage(1, synthetic_text),))


@pytest.fixture
def statement(synthetic_document: ExtractedDocument) -> Statement:
    return SyntheticParser().parse(synthetic_document)


@pytest.fixture
def ofx_reference_statement() -> Statement:
    """Fictitious cent values and accents for the public OFX reference."""
    return Statement(
        bank_id="fictitious", layout_id="ofx-reference",
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 3),
        opening_balance=Decimal("1.00"), closing_balance=Decimal("1.01"),
        transactions=(
            Transaction(date(2026, 9, 1), "CRÉDITO", Decimal("0.01")),
            Transaction(date(2026, 9, 2), "TRANSFERÊNCIA", Decimal("0.01")),
            Transaction(date(2026, 9, 2), "PAGAMENTO JOÃO", Decimal("-0.01")),
        ),
        account=BankAccount(
            organization="Banco Fictício", institution_id="999", bank_id="999",
            branch_id="0001", account_id="00000001",
        ),
    )


@pytest.fixture
def write_pdf() -> Callable[[str, Path], None]:
    # Use the same recipe for committed and negative integration fixtures.
    spec = importlib.util.spec_from_file_location("synthetic_fixture_generator", FIXTURE_DIRECTORY / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_pdf


@pytest.fixture
def inter_pages() -> tuple[str, ...]:
    directory = FIXTURE_DIRECTORY.parent / "inter"
    return tuple((directory / f"page{number}.txt").read_text(encoding="utf-8") for number in (1, 2))


@pytest.fixture
def inter_document(inter_pages: tuple[str, ...]) -> ExtractedDocument:
    return ExtractedDocument(tuple(ExtractedPage(index, text) for index, text in enumerate(inter_pages, 1)))


@pytest.fixture
def inter_statement(inter_document: ExtractedDocument) -> Statement:
    return InterParser().parse(inter_document)


@pytest.fixture
def inter_pdf() -> Path:
    return FIXTURE_DIRECTORY.parent / "inter" / "statement.pdf"


@pytest.fixture
def write_inter_pdf() -> Callable[[tuple[str, ...], Path], None]:
    spec = importlib.util.spec_from_file_location("inter_fixture_generator", FIXTURE_DIRECTORY.parent / "inter" / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_pdf
