"""Public failures, safe to display without exposing document contents."""


class ConversionError(Exception):
    """Base class for expected conversion failures."""


class PDFExtractionError(ConversionError):
    """A local PDF could not be read as usable digital text."""


class UnsupportedLayoutError(ConversionError):
    """No supported bank/layout was identified."""


class StatementParseError(ConversionError):
    """Document text does not match the supported layout grammar."""


class StatementValidationError(ConversionError):
    """Normalized financial data failed validation."""


class RecognizedInvalidStatementError(StatementValidationError):
    """A fully consumed structural hypothesis failed financial validation."""


class InvalidCurrencyError(StatementValidationError, ValueError):
    """A currency code is not a canonical three-letter uppercase code."""


class CurrencyConflictError(StatementValidationError):
    """One financial scope contains contradictory currencies; never converted."""
    capability = "currency_conflict"


class FinancialCoverageError(StatementParseError):
    """Document monetary regions have missing, duplicate or invalid ownership."""


class AmbiguousStatementError(ConversionError):
    """Interpretation or financial evidence cannot determine a safe result."""


class OFXGenerationError(ConversionError):
    """The provisional OFX profile cannot represent the supplied data."""


class MissingOFXMetadataError(OFXGenerationError):
    """Explicit bank/account metadata is incomplete."""


class MissingOFXRequirementsError(OFXGenerationError):
    """A non-identity requirement of the export format is unavailable."""


class UnresolvedCurrencyError(MissingOFXRequirementsError):
    """The statement has no demonstrated currency; export metadata cannot supply one."""


class OFXCurrencyConflictError(OFXGenerationError):
    """The export profile currency contradicts the statement currency."""
