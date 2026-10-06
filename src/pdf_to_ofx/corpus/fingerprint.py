"""Count existing structural observations without retaining document contents."""

from collections import Counter
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path

from pdf_to_ofx.application.convert import StatementAnalysis
from pdf_to_ofx.application.diagnose import diagnose_analysis
from pdf_to_ofx.corpus.privacy import counter_codes, technical_code
from pdf_to_ofx.domain.errors import PDFExtractionError, StatementParseError
from pdf_to_ofx.domain.evidence import AnalysisStatus, FinancialRole
from pdf_to_ofx.generic.operators.candidates import date_candidates
from pdf_to_ofx.generic.operators.context import SUMMARY
from pdf_to_ofx.generic.operators.geometry import observe_geometry
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode
from pdf_to_ofx.generic.semantics import balance_labels, scan_money_regions
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.extractor import extract_pdf


@dataclass(frozen=True, slots=True)
class StructuralFingerprint:
    page_count: int | None = None
    positioned_text_available: bool | None = None
    row_count: int | None = None
    date_candidate_count: int | None = None
    money_region_count: int | None = None
    currency_observations: tuple[str, ...] = ()
    chronology_candidates: tuple[str, ...] | None = None
    analysis_status: str | None = None
    failure_stage: str | None = None
    diagnostic: str | None = None
    blocking_capabilities: tuple[str, ...] = ()
    candidate_hypotheses: int = 0
    explored_hypotheses: int = 0
    pruned_constraints: tuple[tuple[str, int], ...] = ()
    monetary_diagnostic_causes: tuple[tuple[str, int], ...] = ()
    opening_balance_observed: bool | None = None
    closing_balance_observed: bool | None = None
    running_balance_candidate: bool | None = None
    group_subtotal_candidate: bool | None = None
    transaction_count_if_approved: int | None = None
    date_mode_candidate: str | None = None
    amount_mode_candidate: str | None = None
    balance_mode_candidate: str | None = None
    approved_chronology: str | None = None
    approved_role_counts: tuple[tuple[str, int], ...] = ()
    reconciliation_level: str | None = None
    budget_exhausted: bool = False
    observation_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        for field in ("pruned_constraints", "monetary_diagnostic_causes", "approved_role_counts"):
            result[field] = dict(getattr(self, field))
        return result

    @property
    def structural_signature(self) -> str:
        return _signature(self.to_dict())

    @property
    def outcome_signature(self) -> str:
        return _signature({
            "analysis_status": self.analysis_status, "failure_stage": self.failure_stage,
            "diagnostic": self.diagnostic, "blocking_capabilities": self.blocking_capabilities,
            "pruned_constraints": [name for name, _ in self.pruned_constraints],
            "monetary_diagnostic_causes": [name for name, _ in self.monetary_diagnostic_causes],
        })


def _signature(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def fingerprint_analysis(analysis: StatementAnalysis) -> StructuralFingerprint:
    """Project the verdict, SearchLedger summaries and approved provenance."""
    diagnostic = diagnose_analysis(analysis)
    approved = analysis.status == AnalysisStatus.SUCCESS and analysis.statement is not None
    statement = analysis.statement if approved else None
    roles = Counter(a.role.value for a in analysis.provenance.monetary_regions) if approved else Counter()
    return StructuralFingerprint(
        analysis_status=analysis.status.value,
        failure_stage=technical_code(analysis.failure_stage) if analysis.failure_stage else
            ("extraction" if analysis.error_type is PDFExtractionError else "analysis" if not approved else None),
        diagnostic=diagnostic["diagnostic"],
        blocking_capabilities=tuple(sorted({technical_code(c) for c in analysis.blocking_capabilities})),
        candidate_hypotheses=analysis.candidate_hypotheses,
        explored_hypotheses=analysis.explored_hypotheses,
        pruned_constraints=counter_codes(analysis.pruned_constraints),
        monetary_diagnostic_causes=tuple(sorted(Counter(d.cause.value for d in analysis.monetary_diagnostics).items())),
        chronology_candidates=tuple(c.value for c in analysis.chronology_candidates)
            if analysis.chronology_candidates is not None else None,
        transaction_count_if_approved=len(statement.transactions) if statement else None,
        opening_balance_observed=statement.opening_balance is not None if statement else None,
        closing_balance_observed=statement.closing_balance is not None if statement else None,
        approved_chronology=statement.chronology.value if statement else None,
        approved_role_counts=tuple(sorted(roles.items())),
        reconciliation_level=analysis.evidence.reconciliation_level if approved else None,
        budget_exhausted=analysis.budget_exhausted,
    )


def fingerprint_pdf(path: Path, analysis: StatementAnalysis) -> StructuralFingerprint:
    """Observe only; never call inference, choose hypotheses or generate OFX.

    A second extraction supplies lexical counts unavailable on StatementAnalysis.
    Optional observation failures cannot alter the application's verdict.
    """
    fingerprint = fingerprint_analysis(analysis)
    try:
        document = extract_pdf(path)
    except PDFExtractionError:
        return replace(fingerprint, observation_errors=("extraction_unavailable",))
    fingerprint = replace(fingerprint, page_count=len(document.pages),
                          positioned_text_available=all(bool(p.words) for p in document.pages))
    try:
        rows = reconstruct_rows(document)
    except StatementParseError:
        return replace(fingerprint, observation_errors=("rows_unavailable",))
    scans = [scan_money_regions(row, Tolerances()) for row in rows]
    currencies = {region.money.currency.code for scan in scans for region in scan.regions if region.money.currency}
    opening = bool(fingerprint.opening_balance_observed)
    closing = bool(fingerprint.closing_balance_observed)
    for row, scan in zip(rows, scans, strict=True):
        label = " ".join(w.text for w in row.words[:scan.regions[0].start]) if scan.regions else row.text
        named = SUMMARY.get(label.casefold().rstrip(":"))
        labels = balance_labels(label) if label.casefold().startswith("saldo ") else ()
        opening |= bool(named and named[1] == FinancialRole.OPENING_BALANCE) or any(x in {"inicial", "anterior"} for x in labels)
        closing |= bool(named and named[1] == FinancialRole.CLOSING_BALANCE) or any(x in {"final", "total"} for x in labels)
    fingerprint = replace(fingerprint, row_count=len(rows), money_region_count=sum(len(s.regions) for s in scans),
                          currency_observations=tuple(sorted(currencies)),
                          opening_balance_observed=opening, closing_balance_observed=closing)
    errors = []
    try:
        fingerprint = replace(fingerprint, date_candidate_count=sum(len(date_candidates(row)) for row in rows))
    except StatementParseError:
        errors.append("date_candidates_unavailable")
    if fingerprint.money_region_count == 0:
        return replace(fingerprint, running_balance_candidate=False, group_subtotal_candidate=False,
                       observation_errors=tuple(sorted(errors)))
    try:
        profile = observe_geometry(document, None, Tolerances())
        fingerprint = replace(fingerprint, running_balance_candidate=profile.balance_mode == BalanceMode.RUNNING,
                              group_subtotal_candidate=profile.amount_mode == AmountMode.GROUP_SUBTOTAL,
                              date_mode_candidate=profile.date_mode.value, amount_mode_candidate=profile.amount_mode.value,
                              balance_mode_candidate=profile.balance_mode.value)
    except StatementParseError:
        errors.append("geometry_candidates_unavailable")
    return replace(fingerprint, observation_errors=tuple(sorted(errors)))
