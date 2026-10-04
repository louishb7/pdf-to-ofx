"""Shared synthetic inputs, never private bank documents."""

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.domain.models import Statement
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
def write_pdf() -> Callable[[str, Path], None]:
    # Use the same recipe for committed and negative integration fixtures.
    spec = importlib.util.spec_from_file_location("synthetic_fixture_generator", FIXTURE_DIRECTORY / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_pdf
