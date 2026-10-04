"""The reusable local conversion pipeline, without output-file side effects."""

from dataclasses import dataclass
from pathlib import Path

from pdf_to_ofx.banks.detector import detect_parser
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.domain.errors import UnsupportedLayoutError
from pdf_to_ofx.generic.inference import InferenceStatus, infer_layout
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.ofx.generator import OFXProfile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.validation.statement import validate_statement


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    bank_name: str
    statement: Statement
    layout_profile: LayoutProfile | None = None


@dataclass(frozen=True, slots=True)
class ConversionResult:
    bank_name: str
    statement: Statement
    ofx: str


def parse_pdf(path: Path, *, layout_profile: LayoutProfile | None = None,
              context: StatementContext | None = None) -> ParsedStatement:
    """Interpret locally, including valid statements with missing OFX metadata.

    Proven specific parsers stay the default for their recognized layouts. An
    explicit structural profile, supplied context, or an unrecognized bank uses
    the generic path. A failing recognized parser is never bypassed silently.
    """
    document = extract_pdf(path)
    if layout_profile is None and context is None:
        try:
            parser = detect_parser(document)
        except UnsupportedLayoutError:
            pass
        else:
            statement = parser.parse(document)
            validate_statement(statement)
            return ParsedStatement(parser.bank_name, statement)
    if layout_profile is None:
        inference = infer_layout(document, context=context)
        if inference.status != InferenceStatus.SUCCESS:
            raise UnsupportedLayoutError(inference.reason)
        assert inference.profile is not None
        layout_profile = inference.profile
    statement = GenericStatementParser().parse(document, layout_profile, context=context)
    name = statement.account.organization if statement.account is not None else "Instituição não identificada"
    return ParsedStatement(name, statement, layout_profile)


def convert_pdf(path: Path, *, profile: OFXProfile | None = None,
                layout_profile: LayoutProfile | None = None,
                context: StatementContext | None = None) -> ConversionResult:
    parsed = parse_pdf(path, layout_profile=layout_profile, context=context)
    return ConversionResult(parsed.bank_name, parsed.statement, generate_ofx(parsed.statement, profile))
