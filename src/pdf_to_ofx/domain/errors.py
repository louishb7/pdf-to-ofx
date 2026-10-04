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


class OFXGenerationError(ConversionError):
    """The provisional OFX profile cannot represent the supplied data."""
