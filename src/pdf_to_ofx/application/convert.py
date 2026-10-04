"""The reusable local conversion pipeline, without output-file side effects."""

from dataclasses import dataclass
from pathlib import Path

from pdf_to_ofx.banks.detector import detect_parser
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.ofx.generator import OFXProfile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.validation.statement import validate_statement


@dataclass(frozen=True, slots=True)
class ConversionResult:
    bank_name: str
    statement: Statement
    ofx: str


def convert_pdf(path: Path, *, profile: OFXProfile | None = None) -> ConversionResult:
    document = extract_pdf(path)
    parser = detect_parser(document)
    statement = parser.parse(document)
    validate_statement(statement)
    return ConversionResult(parser.bank_name, statement, generate_ofx(statement, profile))
