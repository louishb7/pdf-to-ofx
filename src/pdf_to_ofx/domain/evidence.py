"""Immutable verification outcomes and source references, without copied text."""

from dataclasses import dataclass
from enum import StrEnum

from pdf_to_ofx.domain.models import Statement


class AnalysisStatus(StrEnum):
    SUCCESS = "success"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    NOT_AVAILABLE = "not_available"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    domain_valid: bool = False
    economic_order_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    running_balance_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    running_balance_links: int = 0
    first_movement_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    opening_closing_reconciled: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    group_subtotals_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    credit_total_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    debit_total_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    daily_balances_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE
    financial_coverage_verified: EvidenceStatus = EvidenceStatus.NOT_AVAILABLE

    @property
    def has_financial_support(self) -> bool:
        return (self.opening_closing_reconciled == EvidenceStatus.VERIFIED
                or (self.running_balance_verified == EvidenceStatus.VERIFIED
                    and self.running_balance_links > 0))

    @property
    def reconciliation_level(self) -> str:
        if self.opening_closing_reconciled == EvidenceStatus.VERIFIED:
            return "opening_closing"
        if self.running_balance_verified == EvidenceStatus.VERIFIED and self.running_balance_links:
            return "partial_running_chain"
        return "none"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """One-based page/row; zero-based word interval, end exclusive.

    Visual rows are reconstructed before frame removal. Historical text parsers
    reference nonblank text lines instead, explicitly marked by ``basis``.
    """
    page: int
    row: int
    word_start: int
    word_end: int
    basis: str = "visual_row"
    region_id: str = "main"

    def __post_init__(self) -> None:
        if (any(type(value) is not int for value in (self.page, self.row, self.word_start, self.word_end))
                or self.page < 1 or self.row < 1 or self.word_start < 0 or self.word_end <= self.word_start
                or self.basis not in {"visual_row", "text_line"}
                or not isinstance(self.region_id, str) or not self.region_id):
            raise ValueError("Invalid document source indexes.")


class FinancialRole(StrEnum):
    MOVEMENT = "movement"
    RUNNING_BALANCE = "running_balance"
    OPENING_BALANCE = "opening_balance"
    CLOSING_BALANCE = "closing_balance"
    DAILY_BALANCE = "daily_balance"
    SUBTOTAL = "subtotal"
    CREDIT_TOTAL = "credit_total"
    DEBIT_TOTAL = "debit_total"
    BALANCE_COMPONENT = "balance_component"
    SUMMARY_ADJUSTMENT = "summary_adjustment"
    OTHER_FINANCIAL_CONTROL = "other_financial_control"


@dataclass(frozen=True, slots=True)
class MonetaryAssignment:
    source: SourceSpan
    role: FinancialRole
    transaction_index: int | None = None


@dataclass(frozen=True, slots=True)
class TransactionSource:
    transaction_index: int
    spans: tuple[SourceSpan, ...]
    date_source: SourceSpan | None = None
    description_sources: tuple[SourceSpan, ...] = ()
    amount_source: SourceSpan | None = None
    balance_source: SourceSpan | None = None
    direction_source: SourceSpan | None = None
    direction_basis: str | None = None
    document_order: int | None = None
    economic_order: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentRegion:
    """A classified scope or frame, containing references rather than text."""
    region_id: str
    kind: str
    spans: tuple[SourceSpan, ...]


@dataclass(frozen=True, slots=True)
class ChronologySource:
    document_order: tuple[int, ...]
    economic_order: tuple[int, ...] | None
    basis: str
    date_sources: tuple[SourceSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class Provenance:
    transactions: tuple[TransactionSource, ...] = ()
    monetary_regions: tuple[MonetaryAssignment, ...] = ()
    regions: tuple[DocumentRegion, ...] = ()
    chronology: ChronologySource | None = None

    @property
    def financial_scope(self) -> DocumentRegion | None:
        financial = [region for region in self.regions if region.kind == "financial"]
        return financial[0] if len(financial) == 1 else None


@dataclass(frozen=True, slots=True)
class Interpretation:
    statement: Statement
    evidence: EvidenceReport
    provenance: Provenance
