"""Local analysis is independent of account identity and OFX requirements."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from pdf_to_ofx.banks.detector import detect_parser
from pdf_to_ofx.domain.errors import (
    AmbiguousStatementError, ConversionError, MissingOFXMetadataError,
    MissingOFXRequirementsError, OFXGenerationError, PDFExtractionError,
    StatementParseError, StatementValidationError, UnsupportedLayoutError,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus, DocumentRegion, EvidenceReport, EvidenceStatus, FinancialRole, Provenance
from pdf_to_ofx.domain.models import BankAccount, Statement
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext
from pdf_to_ofx.generic.profile import LayoutProfile
from pdf_to_ofx.ofx.generator import OFXProfile, account_profile, generate_ofx, validate_ofx_export
from pdf_to_ofx.pdf.extractor import extract_pdf


@dataclass(frozen=True, slots=True)
class StatementAnalysis:
    status: AnalysisStatus
    statement: Statement | None = None
    evidence: EvidenceReport = EvidenceReport()
    provenance: Provenance = Provenance()
    layout_profile: LayoutProfile | None = None
    bank_name: str = "Instituição não identificada"
    reason: str = ""
    ambiguities: tuple[str, ...] = ()
    # Keep the public failure category without retaining a traceback/document.
    error_type: type[ConversionError] | None = None
    blocking_capabilities: tuple[str, ...] = ()

    @property
    def financial_scope(self) -> DocumentRegion | None:
        return self.provenance.financial_scope


class ExportStatus(StrEnum):
    EXPORT_METADATA_REQUIRED = "export_metadata_required"
    EXPORT_REQUIREMENTS_MISSING = "export_requirements_missing"
    EXPORT_BLOCKED = "export_blocked"
    READY_TO_EXPORT = "ready_to_export"


@dataclass(frozen=True, slots=True)
class ExportReadiness:
    status: ExportStatus
    issues: tuple[str, ...] = ()


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


def analyze_pdf(path: Path, *, layout_profile: LayoutProfile | None = None,
                context: StatementContext | None = None) -> StatementAnalysis:
    """Extract, interpret, cover and verify locally; never generate OFX.

    Identity supplied in legacy context is deliberately excluded. An explicit
    profile selects a structural grammar; identity never selects a parser.
    Recognized historical parser failures are never bypassed by inference.
    """
    financial_context = replace(context, bank_id="unknown", account=None) if context else None
    name = "Instituição não identificada"
    try:
        document = extract_pdf(path)
        parser = None
        if layout_profile is None:
            try:
                parser = detect_parser(document)
            except UnsupportedLayoutError:
                pass
        if parser is not None:
            name = parser.bank_name
            interpretation = parser.interpret(document)
            if financial_context is not None:
                for field in ("period_start", "period_end", "opening_balance", "closing_balance"):
                    value = getattr(financial_context, field)
                    if value is not None and value != getattr(interpretation.statement, field):
                        raise StatementValidationError("Conflicting statement period or balance declarations.")
        elif layout_profile is not None:
            interpretation = GenericStatementParser().interpret_composed(document, layout_profile, context=financial_context)
        else:
            inference = infer_layout(document, context=financial_context)
            if inference.status != AnalysisStatus.SUCCESS:
                return StatementAnalysis(inference.status, reason=inference.reason,
                    ambiguities=(inference.reason,) if inference.status == AnalysisStatus.AMBIGUOUS else (),
                    blocking_capabilities=inference.blocking_capabilities)
            assert inference.interpretation is not None
            interpretation = inference.interpretation
            layout_profile = inference.profile
        if (not interpretation.evidence.has_financial_support
                or interpretation.evidence.financial_coverage_verified != EvidenceStatus.VERIFIED):
            reason = "Independent financial evidence or complete monetary coverage is insufficient."
            return StatementAnalysis(AnalysisStatus.AMBIGUOUS, evidence=interpretation.evidence,
                provenance=interpretation.provenance, layout_profile=layout_profile,
                bank_name=name, reason=reason, ambiguities=(reason,))
        return StatementAnalysis(AnalysisStatus.SUCCESS, interpretation.statement,
            interpretation.evidence, interpretation.provenance, layout_profile, name)
    except PDFExtractionError:
        return StatementAnalysis(AnalysisStatus.UNSUPPORTED, reason="PDF contains no usable digital text or cannot be read.",
                                 error_type=PDFExtractionError)
    except AmbiguousStatementError as error:
        return StatementAnalysis(AnalysisStatus.AMBIGUOUS, bank_name=name, reason=str(error),
                                 ambiguities=(str(error),), layout_profile=layout_profile)
    except (StatementParseError, StatementValidationError) as error:
        return StatementAnalysis(AnalysisStatus.INVALID, bank_name=name, reason=str(error),
                                 layout_profile=layout_profile,
                                 error_type=StatementParseError if isinstance(error, OperatorFailure) else type(error),
                                 blocking_capabilities=(error.capability,) if isinstance(error, OperatorFailure) else ())


def _approved_statement(analysis: StatementAnalysis) -> Statement:
    if analysis.status != AnalysisStatus.SUCCESS or analysis.statement is None:
        default = {AnalysisStatus.AMBIGUOUS: AmbiguousStatementError,
                   AnalysisStatus.UNSUPPORTED: UnsupportedLayoutError,
                   AnalysisStatus.INVALID: StatementValidationError}
        raise (analysis.error_type or default.get(analysis.status, StatementValidationError))(
            analysis.reason or "Statement analysis is not approved."
        )
    if (not analysis.evidence.domain_valid or not analysis.evidence.has_financial_support
            or analysis.evidence.financial_coverage_verified != EvidenceStatus.VERIFIED):
        raise StatementValidationError("Approved financial evidence and coverage are required.")
    count = len(analysis.statement.transactions)
    if (len(analysis.provenance.transactions) != count
            or sum(region.role == FinancialRole.MOVEMENT for region in analysis.provenance.monetary_regions) != count):
        raise StatementValidationError("Approved transactions differ from their coverage evidence.")
    if (analysis.financial_scope is None or any(source.date_source is None or not source.description_sources
            or source.amount_source is None or source.direction_source is None
            for source in analysis.provenance.transactions)):
        raise StatementValidationError("Complete field provenance and one financial scope are required.")
    return analysis.statement


def assess_export_readiness(analysis: StatementAnalysis,
                            metadata: BankAccount | OFXProfile | None = None) -> ExportReadiness:
    """Check the actual exporter preconditions, without creating its payload."""
    try:
        statement = _approved_statement(analysis)
    except ConversionError:
        return ExportReadiness(ExportStatus.EXPORT_BLOCKED, ("analysis_not_approved",))
    profile = account_profile(metadata) if isinstance(metadata, BankAccount) else metadata
    try:
        validate_ofx_export(statement, profile)
    except MissingOFXMetadataError:
        return ExportReadiness(ExportStatus.EXPORT_METADATA_REQUIRED, ("account_metadata_missing",))
    except MissingOFXRequirementsError:
        return ExportReadiness(ExportStatus.EXPORT_REQUIREMENTS_MISSING, ("closing_balance_missing",))
    except StatementValidationError:
        return ExportReadiness(ExportStatus.EXPORT_BLOCKED, ("financial_validation_failed",))
    except OFXGenerationError:
        return ExportReadiness(ExportStatus.EXPORT_REQUIREMENTS_MISSING, ("export_requirements_invalid",))
    return ExportReadiness(ExportStatus.READY_TO_EXPORT)


def export_analysis(analysis: StatementAnalysis, metadata: BankAccount | OFXProfile | None = None) -> str:
    """Export approved interpretation; identity never changes its financial data."""
    statement = _approved_statement(analysis)
    return generate_ofx(statement, account_profile(metadata) if isinstance(metadata, BankAccount) else metadata)


def parse_pdf(path: Path, *, layout_profile: LayoutProfile | None = None,
              context: StatementContext | None = None) -> ParsedStatement:
    """Compatibility wrapper; legacy context identity is attached after analysis."""
    analysis = analyze_pdf(path, layout_profile=layout_profile, context=context)
    statement = _approved_statement(analysis)
    name = analysis.bank_name
    if context is not None:
        if statement.account is not None and context.account is not None and statement.account != context.account:
            raise OFXGenerationError("Supplied account metadata contradicts the statement account metadata.")
        statement = replace(statement, bank_id=context.bank_id,
                            account=statement.account or context.account)
        if statement.account is not None:
            name = statement.account.organization
    return ParsedStatement(name, statement, analysis.layout_profile)


def convert_pdf(path: Path, *, profile: OFXProfile | None = None,
                layout_profile: LayoutProfile | None = None,
                context: StatementContext | None = None) -> ConversionResult:
    """Traditional CLI convenience: analyze and export, or fail closed."""
    parsed = parse_pdf(path, layout_profile=layout_profile, context=context)
    return ConversionResult(parsed.bank_name, parsed.statement, generate_ofx(parsed.statement, profile))
