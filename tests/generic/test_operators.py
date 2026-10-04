"""Composition, field sources, disjoint ownership and structural invariance."""

from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf, assess_export_readiness, ExportStatus
from pdf_to_ofx.banks.inter import InterParser
from pdf_to_ofx.banks.synthetic import SyntheticParser
from pdf_to_ofx.cli import main
from pdf_to_ofx.domain.errors import AmbiguousStatementError, FinancialCoverageError
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus, FinancialRole, SourceSpan
from pdf_to_ofx.domain.models import BankAccount, Chronology
from pdf_to_ofx.generic.composition import interpret_composed, resolve_hypotheses
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.operators.amounts import infer_amount_roles
from pdf_to_ofx.generic.operators.candidates import OperatorFailure, date_candidates
from pdf_to_ofx.generic.operators.chronology import infer_chronology
from pdf_to_ofx.generic.operators.dates import attribute_dates
from pdf_to_ofx.generic.operators.directions import infer_direction
from pdf_to_ofx.generic.operators.pages import continue_pages
from pdf_to_ofx.generic.operators.scopes import segment_scopes
from pdf_to_ofx.generic.operators.transactions import semantic_candidates, segment_transactions
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.provenance import VisualCoverage
from pdf_to_ofx.generic.semantics import money_regions, parse_date, parse_money
from pdf_to_ofx.generic.structure import Row, Tolerances, reconstruct_rows
from pdf_to_ofx.ofx.generator import generate_ofx
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage, Word
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.geometry import aligned_with, near, normalized_x, normalized_y, same_row
from pdf_to_ofx.pdf.sources import source_tokens
from pdf_to_ofx.validation.coverage import CoverageLedger


ACCOUNT = BankAccount("Instituição Fictícia", "999", "999", "0001", "DEMO-0001")


def positioned(*pages):
    """Synthetic cell coordinates allow deliberate multiline/column boundaries."""
    output = []
    for number, rows in enumerate(pages, 1):
        words = []
        for index, cells in enumerate(rows):
            for left, text in cells:
                for token in text.split():
                    width = len(token) * 5
                    words.append(Word(token, left, left + width, 20 + index * 18, 30 + index * 18, number))
                    left += width + 4
        output.append(ExtractedPage(number, "", tuple(words), 595, 842))
    return ExtractedDocument(tuple(output))


def signed_wrapped_document(mode=DateMode.GROUPED, marker=False):
    amount = "10,00 C" if marker else "+10,00"
    header = [[(20, "DECLARAÇÃO FICTÍCIA")], [(20, "Período: 01/03/2027 a 03/03/2027")],
              [(20, "Saldo inicial: 100,00")], [(20, "Saldo final: 110,00")]]
    date_row = [[(20, "01 MAR 2027")]] if mode == DateMode.GROUPED else []
    movement = [(40, "OPERAÇÃO FICTÍCIA"), (400, amount), (500, "110,00")]
    if mode == DateMode.PER_TRANSACTION:
        movement = [(40, "01/03/2027"), (100, "OPERAÇÃO FICTÍCIA"), (400, amount), (500, "110,00")]
    first = header + date_row + [movement, [(150, "REFERÊNCIA ALFA")], [(20, "Contato demonstração")]]
    second = [[(20, "DECLARAÇÃO FICTÍCIA")], [(150, "REFERÊNCIA BETA")], [(20, "NOTA ADMINISTRATIVA")],
              [(20, "Contato demonstração")]]
    profile = LayoutProfile(mode, AmountMode.CREDIT_DEBIT_MARKER if marker else AmountMode.SIGNED,
        BalanceMode.RUNNING, carry_date_across_pages=True, footer_rows=1,
        transaction_left=40, continuation_left=150,
        repeated_header_rows=1, trailing_note_rows=1)
    return positioned(first, second), profile


@pytest.mark.parametrize("fixture", ["inter_pdf", "grouped_pdf", "coverage_pdf"])
def test_operator_and_legacy_models_and_financial_evidence_are_equivalent(request, fixture):
    document = extract_pdf(request.getfixturevalue(fixture))
    old, new = infer_layout(document, legacy=True), infer_layout(document)
    assert old.status == new.status == AnalysisStatus.SUCCESS
    assert old.interpretation.statement == new.interpretation.statement
    assert old.interpretation.evidence == new.interpretation.evidence
    specific = InterParser().interpret(document) if fixture == "inter_pdf" else None
    if specific:
        normalized = replace(new.interpretation.statement, bank_id=specific.statement.bank_id,
                             account=specific.statement.account, layout_id=specific.statement.layout_id)
        assert normalized == specific.statement
        assert new.interpretation.evidence == specific.evidence


def test_both_structural_date_recipes_have_legacy_equivalence(layout_pdf):
    document = extract_pdf(layout_pdf)
    old, new = infer_layout(document, legacy=True), infer_layout(document)
    assert old.status == new.status == AnalysisStatus.SUCCESS
    assert old.interpretation.statement == new.interpretation.statement
    assert old.interpretation.evidence == new.interpretation.evidence


def test_synthetic_composition_and_specific_export_identical_bytes(synthetic_pdf):
    document = extract_pdf(synthetic_pdf)
    specific = SyntheticParser().interpret(document)
    composed = infer_layout(document).interpretation
    assert composed is not None
    normalized = replace(composed.statement, bank_id=specific.statement.bank_id,
                         account=specific.statement.account, layout_id=specific.statement.layout_id)
    assert normalized == specific.statement
    # Absence of daily controls is recorded as unavailable by composition;
    # the historical M0 grammar declares those controls not applicable.
    assert replace(composed.evidence, daily_balances_verified=EvidenceStatus.NOT_APPLICABLE) == specific.evidence
    assert generate_ofx(normalized) == generate_ofx(specific.statement)


@pytest.mark.parametrize("fixture", ["synthetic_pdf", "inter_pdf", "grouped_pdf", "coverage_pdf"])
@pytest.mark.parametrize("legacy", [False, True])
def test_every_field_resolves_to_its_original_tokens(request, fixture, legacy):
    document = extract_pdf(request.getfixturevalue(fixture))
    if legacy and fixture in {"inter_pdf", "synthetic_pdf"}:
        result = (InterParser() if fixture == "inter_pdf" else SyntheticParser()).interpret(document)
        tolerances = Tolerances()
    else:
        inferred = infer_layout(document, legacy=legacy)
        result, tolerances = inferred.interpretation, inferred.profile.tolerances
    assert result is not None and result.provenance.financial_scope is not None
    for transaction, source in zip(result.statement.transactions, result.provenance.transactions, strict=True):
        assert source.date_source is not None and source.amount_source is not None
        assert parse_date(" ".join(source_tokens(document, source.date_source, tolerances))) == transaction.posting_date
        assert " ".join(token for span in source.description_sources
                        for token in source_tokens(document, span, tolerances)) == transaction.description
        money = parse_money(" ".join(source_tokens(document, source.amount_source, tolerances)))
        assert money.amount.copy_abs() == transaction.amount.copy_abs()
        if transaction.balance_after is not None:
            assert parse_money(" ".join(source_tokens(document, source.balance_source, tolerances))).amount == transaction.balance_after
        assert source.direction_source is not None
        assert source.spans and all(source_tokens(document, span, tolerances) for span in source.spans)
        assert source.document_order == source.economic_order == source.transaction_index
    for assignment in result.provenance.monetary_regions:
        assert parse_money(" ".join(source_tokens(document, assignment.source, tolerances))) is not None


@pytest.mark.parametrize("mode,marker", [(DateMode.GROUPED, False), (DateMode.PER_TRANSACTION, False), (DateMode.PER_TRANSACTION, True)])
def test_multiline_and_page_frames_are_reusable_with_signed_values_and_markers(mode, marker):
    document, profile = signed_wrapped_document(mode, marker)
    result = interpret_composed(document, profile)
    transaction = result.statement.transactions[0]
    assert transaction.description == "OPERAÇÃO FICTÍCIA REFERÊNCIA ALFA REFERÊNCIA BETA"
    assert transaction.amount == Decimal("10.00") and transaction.balance_after == Decimal("110.00")
    source = result.provenance.transactions[0]
    assert len(source.description_sources) == 3
    assert [s.page for s in source.description_sources] == [1, 1, 2]
    assert source.date_source.page == 1
    assert result.provenance.regions[1].kind == "informational"
    assert result.evidence.opening_closing_reconciled == EvidenceStatus.VERIFIED
    assert result.evidence.financial_coverage_verified == EvidenceStatus.VERIFIED
    assert result.evidence.group_subtotals_verified == EvidenceStatus.NOT_APPLICABLE
    assert LayoutProfile.from_json(profile.to_json()) == profile


def test_segmentation_precedes_date_roles_and_direction():
    document, profile = signed_wrapped_document()
    rows = continue_pages(reconstruct_rows(document), profile)
    candidates = semantic_candidates(rows[4:], profile)
    # Changing amount column roles or amount convention cannot alter segments.
    swapped = replace(profile, movement_column=1, balance_column=0)
    assert segment_transactions(candidates, profile) == segment_transactions(candidates, swapped)
    segments = segment_transactions(candidates, profile)
    dated = attribute_dates(segments, candidates, DateMode.GROUPED, carry_across_pages=True)
    roles = infer_amount_roles(segments[0], profile)
    assert roles.magnitude == Decimal("10.00")
    assert dated[0].candidate.value == date(2027, 3, 1)
    direction = infer_direction(segments[0], roles, profile.amount_mode, candidates, profile.tolerances, carry_across_pages=True)
    assert direction.sign == 1 and direction.basis == "explicit_sign"
    # Date attribution consumes only calendar/group context, even with swapped
    # monetary column hypotheses; no amount inference is invoked.
    assert attribute_dates(segments, candidates, swapped.date_mode, carry_across_pages=True) == dated


def test_group_direction_has_its_own_source_instead_of_movement_sign(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    result = infer_layout(document).interpretation
    sources = result.provenance.transactions
    assert all(s.direction_basis == "signed_group_subtotal" for s in sources)
    assert all(s.direction_source != s.amount_source for s in sources)
    for transaction, source in zip(result.statement.transactions, sources):
        control = parse_money(" ".join(source_tokens(document, source.direction_source)))
        assert control.explicit_sign
        assert control.amount.is_signed() == transaction.amount.is_signed()


@pytest.mark.parametrize("mode", [DateMode.GROUPED, DateMode.PER_TRANSACTION])
def test_page_context_abstains_if_disabled_for_a_split_description(mode):
    document, profile = signed_wrapped_document(mode)
    with pytest.raises(OperatorFailure, match="orphan"):
        interpret_composed(document, replace(profile, carry_date_across_pages=False))


@pytest.mark.parametrize("date_text", ["01/03/2027", "01 MAR 2027", "1 de março de 2027"])
def test_full_date_can_appear_inside_description_columns(make_document, date_text):
    document = make_document("Período: 01/03/2027 a 03/03/2027\nSaldo inicial: 0,00\nSaldo final: 1,00\n"
                             f"OPERAÇÃO {date_text} FICTÍCIA +1,00")
    result = infer_layout(document)
    assert result.status == AnalysisStatus.SUCCESS
    assert result.interpretation.statement.transactions[0].description == "OPERAÇÃO FICTÍCIA"
    assert len(result.interpretation.provenance.transactions[0].description_sources) == 2


def test_yearless_date_does_not_borrow_a_year(make_document):
    result = infer_layout(make_document("Período: 01/03/2027 a 03/03/2027\nSaldo inicial: 0,00\nSaldo final: 1,00\n01 MAR OPERAÇÃO +1,00"))
    assert result.status != AnalysisStatus.SUCCESS
    assert "date_attribution" in result.blocking_capabilities


def test_multiple_local_date_candidates_abstain_without_scores(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\nSaldo inicial: 0,00\nSaldo final: 1,00\n"
                             "01/03/2027 OPERAÇÃO 02/03/2027 +1,00")
    row = reconstruct_rows(document)[-1]
    assert len(date_candidates(row)) == 2
    result = infer_layout(document)
    assert result.status == AnalysisStatus.AMBIGUOUS
    with patch("pdf_to_ofx.application.convert.extract_pdf", return_value=document):
        assert analyze_pdf(None).status == AnalysisStatus.AMBIGUOUS


def test_two_surviving_financial_compositions_abstain(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\n"
                             "01/03/2027 ALFA 0,01 -0,01\n02/03/2027 BETA 0,02 0,01")
    a = interpret_composed(document, LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING))
    b = interpret_composed(document, LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING, 1, 0))
    assert a.evidence.has_financial_support and b.evidence.has_financial_support
    with pytest.raises(AmbiguousStatementError):
        resolve_hypotheses((a, b))
    assert resolve_hypotheses((a, a)) == a
    assert infer_layout(document).status == AnalysisStatus.AMBIGUOUS


def test_chronology_never_reorders_document_transactions(statement):
    reversed_transactions = statement.transactions[::-1]
    chronology, source = infer_chronology(reversed_transactions, ())
    assert chronology == Chronology.UNDECLARED
    assert source.economic_order is None
    assert source.document_order == tuple(range(len(reversed_transactions)))
    assert source.basis == "economic_order_unresolved"


def test_reverse_chronology_is_a_local_diagnostic(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\nSaldo inicial: 0,00\nSaldo final: 2,00\n"
                             "02/03/2027 ALFA +1,00\n01/03/2027 BETA +1,00")
    result = infer_layout(document)
    assert result.status != AnalysisStatus.SUCCESS
    assert "chronology_inference" in result.blocking_capabilities


def test_all_monetary_occurrences_have_unique_known_roles(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    result = infer_layout(document)
    coverage = VisualCoverage(reconstruct_rows(document), result.profile.tolerances)
    assignments = result.interpretation.provenance.monetary_regions
    assert {a.source for a in assignments} == coverage.expected
    assert len(assignments) == len(coverage.expected)
    assert {a.role for a in assignments} >= {FinancialRole.MOVEMENT, FinancialRole.SUBTOTAL,
        FinancialRole.OPENING_BALANCE, FinancialRole.CLOSING_BALANCE, FinancialRole.CREDIT_TOTAL, FinancialRole.DEBIT_TOTAL}
    assert "ignore" not in {role.value for role in FinancialRole}


def test_duplicate_and_overlapping_monetary_ownership_are_rejected():
    source = SourceSpan(1, 1, 0, 2)
    coverage = CoverageLedger((source,))
    coverage.claim(source, FinancialRole.MOVEMENT, 0)
    with pytest.raises(FinancialCoverageError):
        coverage.claim(source, FinancialRole.SUBTOTAL)
    with pytest.raises(FinancialCoverageError):
        CoverageLedger((source, SourceSpan(1, 1, 1, 3)))
    with pytest.raises(FinancialCoverageError):
        CoverageLedger((source, replace(source, region_id="another")))


def test_canceling_movements_cannot_be_removed_by_a_candidate_operator(coverage_pdf):
    document = extract_pdf(coverage_pdf)
    profile = infer_layout(document).profile
    real = continue_pages
    def suppress(rows, profile):
        retained = real(rows, profile)
        return tuple(row for row in retained if "CRÉDITO FICTÍCIO" not in row.text and "DÉBITO FICTÍCIO" not in row.text)
    with patch("pdf_to_ofx.generic.composition.continue_pages", side_effect=suppress):
        with pytest.raises(FinancialCoverageError):
            interpret_composed(document, profile)
        assert analyze_pdf(coverage_pdf).status == AnalysisStatus.INVALID


def test_missing_and_wrong_field_ownership_block_completion(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    profile = infer_layout(document).profile
    real = CoverageLedger.field_sources
    def wrong_amount(self, index, **fields):
        fields["amount"] = fields["date"]
        real(self, index, **fields)
    with patch.object(CoverageLedger, "field_sources", wrong_amount):
        with pytest.raises(FinancialCoverageError, match="Field source"):
            interpret_composed(document, profile)


def test_missing_field_provenance_prevents_success(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    profile = infer_layout(document).profile
    with patch.object(CoverageLedger, "field_sources"):
        with pytest.raises(FinancialCoverageError, match="field provenance"):
            interpret_composed(document, profile)


def test_missing_monetary_assignment_prevents_success(grouped_pdf):
    document = extract_pdf(grouped_pdf)
    profile = infer_layout(document).profile
    real = CoverageLedger.claim
    def omit_movement(self, source, role, transaction_index=None):
        if role != FinancialRole.MOVEMENT:
            real(self, source, role, transaction_index)
    with patch.object(CoverageLedger, "claim", omit_movement):
        with pytest.raises(FinancialCoverageError, match="Unclassified monetary"):
            interpret_composed(document, profile)


def test_explicit_scope_partition_preserves_money_and_rejects_two_accounts(make_document):
    document = make_document("CONTA FICTÍCIA A 1,00\nCONTA FICTÍCIA B 2,00\nINFORMAÇÃO LOCAL")
    rows = reconstruct_rows(document)
    coverage = VisualCoverage(rows, Tolerances())
    scopes, frames = segment_scopes(rows, rows[:2], coverage, (("account_a", rows[:1]), ("account_b", rows[1:2])))
    assert [s.region.region_id for s in scopes] == ["account_a", "account_b"]
    assert frames[0].kind == "informational"
    assert {s.region_id for s in coverage.expected} == {"account_a", "account_b"}
    assert scopes[0].region.spans[0].region_id == "account_a"
    for row in rows[:2]:
        coverage.monetary(row, money_regions(row, Tolerances())[0], FinancialRole.OTHER_FINANCIAL_CONTROL)
    # A ledger cannot finish a StatementAnalysis spanning two financial scopes.
    coverage.regions = tuple(s.region for s in scopes) + frames
    from pdf_to_ofx.domain.models import Statement
    from pdf_to_ofx.domain.evidence import EvidenceReport
    with pytest.raises(FinancialCoverageError, match="one coherent"):
        coverage.finish(Statement("unknown", "test", date(2027, 1, 1), date(2027, 1, 1), None, None, ()), EvidenceReport())


@pytest.mark.parametrize("bad_partition", ["duplicate", "missing", "duplicate_name"])
def test_scope_partition_requires_disjoint_complete_rows(make_document, bad_partition):
    rows = reconstruct_rows(make_document("ALFA 1,00\nBETA 2,00"))
    parts = {"duplicate": (("a", rows), ("b", rows)), "missing": (("a", rows[:1]),),
             "duplicate_name": (("a", rows[:1]), ("a", rows[1:]))}[bad_partition]
    with pytest.raises(OperatorFailure, match="partition"):
        segment_scopes(rows, rows, VisualCoverage(rows, Tolerances()), parts)


def test_amount_roles_are_independent_of_date_and_direction():
    document, profile = signed_wrapped_document()
    candidates = semantic_candidates(continue_pages(reconstruct_rows(document), profile)[4:], profile)
    segment = segment_transactions(candidates, profile)[0]
    head = segment.rows[0]
    altered = replace(segment, rows=(replace(head, dates=()), *segment.rows[1:]))
    assert infer_amount_roles(altered, profile) == infer_amount_roles(segment, profile)
    assert infer_direction(segment, infer_amount_roles(segment, profile), profile.amount_mode,
                           (), profile.tolerances, carry_across_pages=False).apply(Decimal("10.00")) == Decimal("10.00")


def test_composition_does_not_invoke_complete_legacy_grammars(grouped_pdf, inter_pdf):
    for path in (grouped_pdf, inter_pdf):
        document = extract_pdf(path)
        profile = infer_layout(document, legacy=True).profile
        with patch.object(GenericStatementParser, "interpret", side_effect=AssertionError("Legacy grammar")), \
             patch("pdf_to_ofx.generic.grouped.interpret_grouped_subtotals", side_effect=AssertionError("Legacy grammar")):
            assert interpret_composed(document, profile).evidence.financial_coverage_verified == EvidenceStatus.VERIFIED


def test_composed_cli_uses_actual_pdf_and_deterministic_ofx(synthetic_pdf, tmp_path, capsys):
    document = extract_pdf(synthetic_pdf)
    specific = SyntheticParser().parse(document)
    result = infer_layout(document)
    # The CLI still has the same interface. Provide identity separately, after
    # structural analysis, as required for export of an unknown institution.
    def composed_conversion(path):
        from pdf_to_ofx.application.convert import ConversionResult
        analysis = analyze_pdf(path, layout_profile=result.profile)
        statement = replace(analysis.statement, bank_id=specific.bank_id, account=specific.account, layout_id=specific.layout_id)
        return ConversionResult("Fictitious", statement, generate_ofx(statement))
    with patch("pdf_to_ofx.cli.convert_pdf", side_effect=composed_conversion):
        first, second = tmp_path / "one.ofx", tmp_path / "two.ofx"
        assert main([str(synthetic_pdf), "-o", str(first)]) == 0
        assert main([str(synthetic_pdf), "-o", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes() == generate_ofx(specific).encode("ascii")
    assert "PIX RECEBIDO" not in capsys.readouterr().out


def test_composition_remains_offline_and_uses_decimal_under_low_precision(grouped_pdf):
    with localcontext() as context, patch("socket.socket", side_effect=AssertionError("Network forbidden")):
        context.prec = 2
        analysis = analyze_pdf(grouped_pdf)
    assert analysis.status == AnalysisStatus.SUCCESS
    assert all(isinstance(t.amount, Decimal) for t in analysis.statement.transactions)


def test_normalized_coordinates_preserve_original_geometry(synthetic_pdf):
    page = extract_pdf(synthetic_pdf).pages[0]
    word = page.words[0]
    assert page.width == 595 and page.height == 842
    assert normalized_x(word, page) == word.x0 / page.width
    assert normalized_y(word, page) == word.top / page.height
    moved = replace(word, x0=word.x0 + 1, x1=word.x1 + 1, top=word.top + 1, bottom=word.bottom + 1)
    assert same_row(word, moved) and aligned_with(word, moved) and near(word, moved)
    assert not same_row(word, replace(moved, page=2))
    assert not near(word, replace(moved, page=2))
    assert not near(word, replace(moved, x0=word.x0 + 100))
    with pytest.raises(ValueError):
        normalized_x(word, replace(page, width=None))
    with pytest.raises(ValueError):
        normalized_y(word, replace(page, height=0))
    with pytest.raises(ValueError):
        normalized_x(replace(word, page=2), page)


def test_invalid_source_reference_abstains(synthetic_pdf):
    with pytest.raises(ValueError, match="interval"):
        source_tokens(extract_pdf(synthetic_pdf), SourceSpan(1, 999, 0, 1))


@pytest.mark.parametrize("marker", [False, True])
def test_printed_pagination_is_reusable_outside_subtotal_mode(marker):
    document, profile = signed_wrapped_document(marker=marker)
    pages = []
    for page in document.pages:
        footer_top = max(w.top for w in page.words)
        existing = tuple(w for w in page.words if w.top != footer_top)
        footer = positioned([[(20, f"Documento gerado em 1 de abril de 2027 {page.number} de 2")]]).pages[0].words
        footer = tuple(replace(w, page=page.number, top=footer_top, bottom=footer_top + 10) for w in footer)
        pages.append(replace(page, words=(*existing, *footer)))
    paginated = replace(document, pages=tuple(pages))
    assert interpret_composed(paginated, profile).statement == interpret_composed(document, profile).statement
    final_page = pages[-1]
    bad = replace(final_page, words=tuple(replace(w, text="3") if w is final_page.words[-1] else w for w in final_page.words))
    with pytest.raises(OperatorFailure, match="pagination"):
        interpret_composed(replace(paginated, pages=(*pages[:-1], bad)), profile)


@pytest.mark.parametrize("helper", [same_row, aligned_with, near])
@pytest.mark.parametrize("tolerance", [-1, float("nan"), True])
def test_geometry_helpers_reject_invalid_tolerances(helper, tolerance):
    word = Word("FICTITIOUS", 20, 40, 20, 30, 1)
    with pytest.raises(ValueError, match="tolerance"):
        helper(word, word, tolerance)
