"""Compose local structural operators; retain legacy summary readers for M7.

Each stage consumes candidates from the previous stage. This module never calls
a complete legacy grammar, consults institution identity or ranks financial data.
"""

from pdf_to_ofx.domain.errors import AmbiguousStatementError, StatementParseError
from pdf_to_ofx.domain.evidence import FinancialRole, Interpretation
from pdf_to_ofx.domain.models import Chronology, Statement, Transaction
from pdf_to_ofx.generic.operators.amounts import infer_amount_roles, infer_control_roles
from pdf_to_ofx.generic.operators.candidates import OperatorFailure
from pdf_to_ofx.generic.operators.chronology import infer_chronology
from pdf_to_ofx.generic.operators.dates import attribute_dates
from pdf_to_ofx.generic.operators.directions import infer_direction
from pdf_to_ofx.generic.operators.financial import financial_evidence
from pdf_to_ofx.generic.operators.pages import continue_pages
from pdf_to_ofx.generic.operators.scopes import segment_scopes
from pdf_to_ofx.generic.operators.transactions import description_intervals, semantic_candidates, segment_transactions
from pdf_to_ofx.generic.parser import StatementContext, read_context
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.structure import reconstruct_rows
from pdf_to_ofx.pdf.document import ExtractedDocument


def resolve_hypotheses(hypotheses: tuple[Interpretation, ...]) -> Interpretation:
    """Provenance syntax is immaterial; distinct financial outcomes abstain."""
    unique = {}
    for hypothesis in hypotheses:
        s = hypothesis.statement
        key = (s.period_start, s.period_end, s.opening_balance, s.closing_balance,
               s.transactions, s.chronology, hypothesis.evidence,
               tuple((a.source, a.role, a.transaction_index) for a in hypothesis.provenance.monetary_regions))
        unique.setdefault(key, hypothesis)
    if len(unique) > 1:
        raise AmbiguousStatementError("Material financial interpretations cannot be distinguished uniquely.")
    if not unique:
        raise OperatorFailure("financial_evidence", "No financial hypothesis survived the constraints.")
    return next(iter(unique.values()))


def interpret_composed(document: ExtractedDocument, profile: LayoutProfile,
                       *, context: StatementContext | None = None) -> Interpretation:
    original = reconstruct_rows(document, profile.tolerances)
    coverage = VisualCoverage(original, profile.tolerances)
    try:
        rows = continue_pages(original, profile)
    except StatementParseError as error:
        raise OperatorFailure("page_continuation", str(error)) from error
    scopes, frames = segment_scopes(original, rows, coverage)
    if len(scopes) != 1:
        raise OperatorFailure("scope_segmentation", "Automatic conversion requires one coherent financial scope.")
    coverage.regions = tuple(s.region for s in scopes) + frames
    try:
        candidates = semantic_candidates(scopes[0].rows, profile)
    except OperatorFailure:
        raise
    except StatementParseError as error:
        raise OperatorFailure("semantic_candidates", str(error)) from error
    start = next((c.position for c in candidates if c.dates and (
        c.kind in {"date_heading", "flow"} or c.kind == "content" and c.amounts)), None)
    if start is None:
        raise OperatorFailure("date_attribution", "No supported full-year dated transaction area was found.")
    # Summary readers are the remaining narrow legacy capability: declarations
    # are outside transaction segmentation, still owned by the same scope.
    body = tuple(c for c in candidates[start:] if c.kind != "balance_declaration")
    declarations = tuple(c.row for c in candidates[:start]) + tuple(
        c.row for c in candidates[start:] if c.kind == "balance_declaration")
    try:
        if profile.amount_mode == AmountMode.GROUP_SUBTOTAL:
            from pdf_to_ofx.generic.grouped import _context
            context, totals = _context(declarations, profile, context, coverage)
        else:
            context = read_context(declarations, profile, context, coverage)
            totals = {}
    except StatementParseError as error:
        raise OperatorFailure("financial_context", str(error)) from error
    segments = segment_transactions(body, profile)
    dated = attribute_dates(segments, body, profile.date_mode,
                            carry_across_pages=profile.carry_date_across_pages)
    transactions = []
    directions = []
    date_sources = []
    for index, item in enumerate(dated):
        segment = item.segment
        roles = infer_amount_roles(segment, profile)
        direction = infer_direction(segment, roles, profile.amount_mode, body, profile.tolerances,
                                    carry_across_pages=profile.carry_date_across_pages)
        directions.append(direction)
        own_date = item.candidate if item.source_row.position == segment.position else None
        description_regions = description_intervals(segment, own_date,
            reject_balance_heading=profile.amount_mode != AmountMode.GROUP_SUBTOTAL)
        description = " ".join(w.text for row, begin, end in description_regions for w in row.words[begin:end])
        head = segment.rows[0].row
        movement_source = coverage.span(head, roles.movement.start, roles.movement.end)
        balance_source = (coverage.span(head, roles.running_balance.start, roles.running_balance.end)
                          if roles.running_balance else None)
        date_source = coverage.span(item.source_row.row, item.candidate.start, item.candidate.end)
        date_sources.append(date_source)
        direction_source = coverage.span(direction.source_row.row, direction.word_start, direction.word_end)
        coverage.monetary(head, roles.movement, FinancialRole.MOVEMENT, index)
        if roles.running_balance:
            coverage.monetary(head, roles.running_balance, FinancialRole.RUNNING_BALANCE, index)
        coverage.transaction(date_source, direction_source, *(coverage.span(c.row) for c in segment.rows))
        coverage.field_sources(index, date=date_source,
            description=tuple(coverage.span(row, begin, end) for row, begin, end in description_regions),
            amount=movement_source, balance=balance_source, direction=direction_source,
            direction_basis=direction.basis, economic_order=None)
        transactions.append(Transaction(item.candidate.value, description, direction.apply(roles.magnitude),
                            roles.running_balance.money.amount if roles.running_balance else None))
    for candidate in body:
        for control in infer_control_roles(candidate, profile.balance_mode):
            coverage.monetary(candidate.row, control.region, control.role)
        if candidate.kind == "date_heading":
            if profile.date_mode == DateMode.GROUPED and profile.amount_mode != AmountMode.GROUP_SUBTOTAL:
                if not any(item.group_position == candidate.position for item in dated):
                    raise OperatorFailure("transaction_segmentation", "A date group contains no transactions.")
    chronology, chronology_source = infer_chronology(tuple(transactions), tuple(date_sources))
    coverage.chronology = chronology_source
    if chronology == Chronology.UNDECLARED:
        raise OperatorFailure("chronology_inference", "Economic order is unresolved; reverse ordering is not supported yet.")
    for index in coverage.fields:
        coverage.fields[index]["economic_order"] = index
    statement = Statement(context.bank_id, "generic-structural-v1", context.period_start,
        context.period_end, context.opening_balance, context.closing_balance, tuple(transactions), context.account,
        chronology=chronology, running_balances_required=profile.balance_mode == BalanceMode.RUNNING)
    evidence = financial_evidence(statement, body, tuple(item.group_position for item in dated),
        tuple(direction.group_position for direction in directions), totals, profile.balance_mode)
    return coverage.finish(statement, evidence)
