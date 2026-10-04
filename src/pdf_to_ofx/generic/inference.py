"""Enumerate small structural hypotheses; accept only a unique validated one."""

from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby
import re

from pdf_to_ofx.domain.errors import StatementParseError, StatementValidationError
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext, leading_date
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import has_financial_signal
from pdf_to_ofx.generic.structure import Row, Tolerances, reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


class InferenceStatus(StrEnum):
    SUCCESS = "success"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class InferenceResult:
    status: InferenceStatus
    profile: LayoutProfile | None
    reason: str


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
    try:
        rows = reconstruct_rows(document, tolerances)
        footer_rows = _contact_footer_rows(rows)
    except StatementParseError:
        return InferenceResult(InferenceStatus.UNSUPPORTED, None, "Ordered positioned words are required.")
    accepted = []
    parser = GenericStatementParser()
    for date_mode in DateMode:
        for amount_mode in AmountMode:
            for balance_mode in BalanceMode:
                columns = ((0, 1), (1, 0)) if balance_mode == BalanceMode.RUNNING else ((0, None),)
                for movement_column, balance_column in columns:
                    profile = LayoutProfile(
                        date_mode, amount_mode, balance_mode, movement_column, balance_column,
                        carry_date_across_pages=date_mode == DateMode.GROUPED,
                        footer_rows=footer_rows, tolerances=tolerances,
                    )
                    try:
                        statement = parser.parse(document, profile, context=context)
                    except (StatementParseError, StatementValidationError):
                        continue
                    # One row without an opening balance has no arithmetic link.
                    # Absent-balance schemas need independent full reconciliation.
                    if balance_mode == BalanceMode.RUNNING:
                        if len(statement.transactions) < 2 and statement.opening_balance is None:
                            continue
                    elif statement.opening_balance is None or statement.closing_balance is None:
                        continue
                    accepted.append(profile)
    if len(accepted) == 1:
        return InferenceResult(InferenceStatus.SUCCESS, accepted[0], "A unique schema passed structural and exact financial validation.")
    if len(accepted) > 1:
        return InferenceResult(InferenceStatus.AMBIGUOUS, None, "Movement and balance roles cannot be distinguished uniquely.")
    return InferenceResult(InferenceStatus.UNSUPPORTED, None, "No schema passed structural and exact financial validation with sufficient evidence.")
