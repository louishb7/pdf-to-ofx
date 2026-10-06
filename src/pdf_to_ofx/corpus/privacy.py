"""Allow only engine vocabulary in reports; unknown labels become opaque hashes."""

import hashlib
import re

from pdf_to_ofx.domain.evidence import AnalysisStatus, FinancialRole, InferenceDiagnostic
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.generic.operators.amounts import EmptyDomainCause
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode


# Existing engine capabilities/constraints and lab stages. This is a reporting
# vocabulary, not a parser registry or a claim that every capability is supported.
TECHNICAL_CODES = frozenset("""
amount_role_inference balance_label_binding balance_semantics_unknown
checkpoint_domain checkpoint_origin checkpoint_same_boundary checkpoint_reconciliation
checkpoint_interval_missing chronology_dates chronology_inference chronology_unknown
circular_financial_control column_role_continuity control_column_ownership
control_economic_frontier control_interval_domain control_interval_membership
control_movement_repeated control_origin control_reference_missing control_reference_order
control_scope_or_membership control_source_reused currency_conflict daily_checkpoint_date
date_attribution date_period declared_credits declared_debits direction_inference direction_origin
exclusive_description_origin exclusive_monetary_ownership financial_caption_ownership financial_reconciliation
financial_context financial_coverage financial_declarations financial_evidence
monetary_coverage monetary_domain_membership monetary_origin monetary_role_domain_empty
monetary_token_incomplete page_continuation pagination_consistency period_declaration_missing
scope_and_segment_origin scope_boundary_unknown scope_segmentation scope_selection
search_budget semantic_candidates subtotal_direction subtotal_interval_overlap
subtotal_partial_bound subtotal_reconciliation summary_adjustment_undetailed
summary_value_binding transaction_segmentation visual_structure
geometry structural_composition local_candidates monetary_inventory extraction analysis
extraction_unavailable rows_unavailable date_candidates_unavailable
geometry_candidates_unavailable observation_error
opening_closing partial_running_chain checkpoint_chain group_subtotals declared_totals none
""".split()) | frozenset(member.value for enum in (
    AnalysisStatus, FinancialRole, InferenceDiagnostic, Chronology, EmptyDomainCause,
    AmountMode, BalanceMode, DateMode,
) for member in enum)


def opaque_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def technical_code(value: str) -> str:
    if value in TECHNICAL_CODES or re.fullmatch(r"unknown_[0-9a-f]{64}", value):
        return value
    return "unknown_" + opaque_hash(value)


def counter_codes(items: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((technical_code(code), count) for code, count in items))
