"""Enumerate small structural hypotheses; accept only a unique validated one."""

from dataclasses import dataclass, replace
from itertools import groupby
import re

from pdf_to_ofx.domain.errors import (
    FinancialCoverageError, RecognizedInvalidStatementError,
    StatementParseError, StatementValidationError,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus, Interpretation
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext, leading_date
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import has_financial_signal
from pdf_to_ofx.generic.structure import Row, Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


InferenceStatus = AnalysisStatus


@dataclass(frozen=True, slots=True)
class InferenceResult:
    status: InferenceStatus
    profile: LayoutProfile | None
    reason: str
    interpretation: Interpretation | None = None


def _contact_footer_rows(rows: tuple[Row, ...]) -> int:
    counts = []
    for _, grouped in groupby(rows, key=lambda row: row.page):
        page = list(grouped)
        last = page[-1]
        # Recognize contact roles conservatively. Never memorize document text
        # or infer arbitrary repeated descriptions as ignorable financial rows.
        labels = set(re.findall(r"\b(?:sac|ouvidoria|telefone|atendimento)\b", last.text.casefold()))
        count = 0
        if len(labels) >= 2 and not has_financial_signal(last.text) and leading_date(last) is None:
            count = 1
            if len(page) >= 2:
                previous = page[-2]
                if (re.match(r"^(?:fale|contato|atendimento)\b", previous.text, re.IGNORECASE)
                        and not has_financial_signal(previous.text) and leading_date(previous) is None):
                    count = 2
        counts.append(count)
    return counts[0] if counts and len(set(counts)) == 1 else 0


def infer_layout(document: ExtractedDocument, *, context: StatementContext | None = None,
                 tolerances: Tolerances = Tolerances()) -> InferenceResult:
    if context is not None:
        # Identity can neither select a grammar nor change financial acceptance.
        context = replace(context, bank_id="unknown", account=None)
    try:
        rows = reconstruct_rows(document, tolerances)
        footer_rows = _contact_footer_rows(rows)
    except StatementParseError:
        return InferenceResult(InferenceStatus.UNSUPPORTED, None, "Ordered positioned words are required.")
    accepted: list[tuple[LayoutProfile, Interpretation]] = []
    invalid = False
    insufficient = False
    parser = GenericStatementParser()

    def try_profile(profile: LayoutProfile) -> None:
        nonlocal invalid, insufficient
        try:
            interpretation = parser.interpret(document, profile, context=context)
        except (RecognizedInvalidStatementError, FinancialCoverageError):
            invalid = True
        except (StatementParseError, StatementValidationError):
            pass
        else:
            if interpretation.evidence.has_financial_support:
                accepted.append((profile, interpretation))
            else:
                insufficient = True
    for date_mode in DateMode:
        for amount_mode in AmountMode:
            if amount_mode == AmountMode.GROUP_SUBTOTAL:
                continue
            for balance_mode in BalanceMode:
                columns = ((0, 1), (1, 0)) if balance_mode == BalanceMode.RUNNING else ((0, None),)
                for movement_column, balance_column in columns:
                    profile = LayoutProfile(
                        date_mode, amount_mode, balance_mode, movement_column, balance_column,
                        carry_date_across_pages=date_mode == DateMode.GROUPED,
                        footer_rows=footer_rows, tolerances=tolerances,
                    )
                    try_profile(profile)
    from pdf_to_ofx.generic.grouped import infer_grouped_profile
    grouped = infer_grouped_profile(rows, tolerances)
    if grouped is not None:
        try_profile(grouped)
    # Different profile syntax is not material ambiguity when financial output,
    # evidence and ownership are identical. Identity is excluded from this key.
    unique = {}
    for profile, interpretation in accepted:
        statement = interpretation.statement
        key = (statement.period_start, statement.period_end, statement.opening_balance,
               statement.closing_balance, statement.transactions,
               interpretation.evidence, interpretation.provenance)
        unique.setdefault(key, (profile, interpretation))
    if len(unique) == 1:
        profile, interpretation = next(iter(unique.values()))
        return InferenceResult(InferenceStatus.SUCCESS, profile, "A unique interpretation passed coverage and financial evidence policy.", interpretation)
    if len(unique) > 1:
        return InferenceResult(InferenceStatus.AMBIGUOUS, None, "Material financial interpretations cannot be distinguished uniquely.")
    if insufficient:
        return InferenceResult(InferenceStatus.AMBIGUOUS, None, "The document lacks independent financial evidence for safe acceptance.")
    if invalid:
        return InferenceResult(InferenceStatus.INVALID, None, "A consumed structural hypothesis contains inconsistent financial data or incomplete coverage.")
    return InferenceResult(InferenceStatus.UNSUPPORTED, None, "No schema passed structural and exact financial validation with sufficient evidence.")
