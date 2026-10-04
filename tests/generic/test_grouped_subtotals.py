"""Generic group direction, wrapped cells and mathematical proof, without OFX input."""

from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import convert_pdf, parse_pdf
from pdf_to_ofx.domain.errors import OFXGenerationError, StatementParseError, StatementValidationError
from pdf_to_ofx.domain.models import BankAccount
from pdf_to_ofx.generic.inference import InferenceStatus, infer_layout
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.generic.semantics import parse_date
from pdf_to_ofx.generic.structure import Tolerances
from pdf_to_ofx.ofx.generator import generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.document import ExtractedDocument

RECIPE = Path(__file__).parents[1] / "fixtures/layouts/grouped_subtotals_wrapped/pages.json"


@pytest.fixture
def flow_pdf(write_layout_pdf, tmp_path):
    output = tmp_path / "grouped.pdf"
    write_layout_pdf(RECIPE, output)
    return output


@pytest.fixture
def changed_document(write_layout_pdf, tmp_path):
    def build(change):
        recipe = json.loads(RECIPE.read_text())
        change(recipe["pages"])
        source, pdf = tmp_path / "recipe.json", tmp_path / "changed.pdf"
        source.write_text(json.dumps(recipe, ensure_ascii=False))
        write_layout_pdf(source, pdf)
        return extract_pdf(pdf)
    return build


def test_grouped_unsigned_flows_are_inferred_from_pdf_alone(flow_pdf):
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("pdf_to_ofx.application.convert.generate_ofx", side_effect=AssertionError("OFX comparison forbidden")):
        parsed = parse_pdf(flow_pdf)
    profile, statement = parsed.layout_profile, parsed.statement
    assert profile is not None and profile.amount_mode == AmountMode.GROUP_SUBTOTAL
    assert profile.balance_mode == BalanceMode.ABSENT
    assert profile.footer_rows == 3 and profile.repeated_header_rows == 2
    assert profile.trailing_note_rows == 2
    assert abs(profile.transaction_left - 110) < 1
    assert abs(profile.continuation_left - 240) < 1
    assert [t.amount for t in statement.transactions] == [Decimal("10.00"), Decimal("10.00"), Decimal("-5.00"), Decimal("-10.00")]
    assert [t.posting_date for t in statement.transactions] == [date(2027, 4, 1)] * 2 + [date(2027, 4, 2)] * 2
    assert [t.description for t in statement.transactions] == [
        "Crédito CLIENTE FICTÍCIO REFERÊNCIA ALFA",
        "Crédito CLIENTE DEMONSTRAÇÃO REFERÊNCIA BETA",
        "Débito FORNECEDOR FICTÍCIO CONTA DEMONSTRAÇÃO",
        "Débito SERVIÇO LOCAL REFERÊNCIA GAMA",
    ]
    assert all(t.balance_after is None for t in statement.transactions)
    assert statement.opening_balance == Decimal("100.00") and statement.closing_balance == Decimal("105.00")
    assert statement.period_start == date(2027, 4, 1) and statement.period_end == date(2027, 4, 30)
    assert statement.bank_id == "unknown" and statement.account is None


def test_group_direction_does_not_depend_on_description_words(changed_document):
    def change(pages):
        for page in pages:
            for row in page:
                for cell in row["cells"]:
                    if cell[0] == 110 and cell[1] in {"Crédito", "Débito"}:
                        cell[1] = "OPERAÇÃO"
    document = changed_document(change)
    result = infer_layout(document)
    assert result.status == InferenceStatus.SUCCESS and result.profile is not None
    assert [t.amount for t in GenericStatementParser().parse(document, result.profile).transactions] == [
        Decimal("10.00"), Decimal("10.00"), Decimal("-5.00"), Decimal("-10.00"),
    ]


def test_grouped_profile_roundtrip_and_deterministic_export(flow_pdf):
    document = extract_pdf(flow_pdf)
    profile = infer_layout(document).profile
    assert profile is not None
    assert LayoutProfile.from_json(profile.to_json()) == profile
    statement = GenericStatementParser().parse(document, LayoutProfile.from_json(profile.to_json()))
    with pytest.raises(OFXGenerationError, match="metadata"):
        generate_ofx(statement)
    account = BankAccount("Instituição Fictícia", "999", "999", "0001", "DEMO-0001")
    first = convert_pdf(flow_pdf, context=StatementContext(bank_id="unknown", account=account))
    second = convert_pdf(flow_pdf, context=StatementContext(bank_id="unknown", account=account))
    assert first.ofx.encode("ascii") == second.ofx.encode("ascii")


@pytest.mark.parametrize("before,after", [
    ("+20,00", "+21,00"), ("-15,00", "-14,00"),
    ("105,00", "106,00"), ("10,00", "9,99"),
    ("+20,00", "20,00"), ("-15,00", "+15,00"),
    ("+20,00", "+R$ BAD"), ("10,00", "-10,00"),
])
def test_weak_or_inconsistent_flow_hypotheses_are_rejected(changed_document, before, after):
    def change(pages):
        for page in pages:
            for row in page:
                for cell in row["cells"]:
                    if cell[1] == before:
                        cell[1] = after
    assert infer_layout(changed_document(change)).status == InferenceStatus.UNSUPPORTED


def test_explicit_profile_cannot_bypass_a_subtotal_mismatch(flow_pdf, changed_document):
    profile = infer_layout(extract_pdf(flow_pdf)).profile
    assert profile is not None
    document = changed_document(lambda pages: pages[1][4]["cells"][-1].__setitem__(1, "4,00"))
    with pytest.raises(StatementValidationError, match="subtotal"):
        GenericStatementParser().parse(document, profile)


@pytest.mark.parametrize("boundary", ["header", "footer", "notes", "continuation"])
def test_frames_and_continuations_cannot_suppress_monetary_content(flow_pdf, changed_document, boundary):
    profile = infer_layout(extract_pdf(flow_pdf)).profile
    assert profile is not None
    positions = {"header": (1, 0), "footer": (1, -2), "notes": (2, 3), "continuation": (1, 2)}
    def change(pages):
        page, row = positions[boundary]
        pages[page][row]["cells"][0][1] += " R$ 0,01"
    document = changed_document(change)
    with pytest.raises((StatementParseError, StatementValidationError)):
        GenericStatementParser().parse(document, profile)
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


def test_missing_amount_row_cannot_be_ignored_as_notes(changed_document):
    def change(pages):
        pages[2].insert(3, {"y": 100, "cells": [[110, "OPERAÇÃO SEM VALOR"]]})
    assert infer_layout(changed_document(change)).status == InferenceStatus.UNSUPPORTED


def test_repeated_period_conflict_fails(flow_pdf, changed_document):
    profile = infer_layout(extract_pdf(flow_pdf)).profile
    assert profile is not None
    document = changed_document(lambda pages: pages[1][1]["cells"][0].__setitem__(1, "01 de abril de 2027 a 29 de abril de 2027"))
    with pytest.raises(StatementParseError, match="headers differ"):
        GenericStatementParser().parse(document, profile)
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


def test_unclassified_financial_header_cannot_disappear(changed_document):
    def change(pages):
        pages[0].insert(8, {"y": 165, "cells": [[35, "AJUSTE NÃO CLASSIFICADO R$ 0,01"]]})
    assert infer_layout(changed_document(change)).status == InferenceStatus.UNSUPPORTED


def test_nonzero_undetailed_yield_fails_closed(changed_document):
    def change(pages):
        pages[0].insert(8, {"y": 165, "cells": [[300, "Rendimento líquido"], [500, "+0,01"]]})
    assert infer_layout(changed_document(change)).status == InferenceStatus.UNSUPPORTED


def test_without_declared_opening_or_closing_balance_inference_fails(changed_document):
    def change(pages):
        del pages[0][2]
    assert infer_layout(changed_document(change)).status == InferenceStatus.UNSUPPORTED


def test_context_cannot_override_pdf_declarations(flow_pdf):
    document = extract_pdf(flow_pdf)
    profile = infer_layout(document).profile
    assert profile is not None
    with pytest.raises(StatementValidationError, match="Conflicting"):
        GenericStatementParser().parse(document, profile, context=StatementContext(opening_balance=Decimal("99.00")))


def test_group_reconciliation_is_independent_of_decimal_context(flow_pdf):
    with localcontext() as context:
        context.prec = 2
        assert len(parse_pdf(flow_pdf).statement.transactions) == 4


def test_page_context_cannot_be_disabled_for_continuation(flow_pdf):
    document = extract_pdf(flow_pdf)
    profile = infer_layout(document).profile
    assert profile is not None
    with pytest.raises(StatementParseError, match="direction group"):
        GenericStatementParser().parse(document, replace(profile, carry_date_across_pages=False))


@pytest.mark.parametrize("text,expected", [
    ("01 ABR 2027", date(2027, 4, 1)), ("2 fev 2028", date(2028, 2, 2)),
    ("31 FEV 2027", None), ("01 XYZ 2027", None),
])
def test_abbreviated_month_dates(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("changes", [
    {"transaction_left": None}, {"continuation_left": float("nan")},
    {"continuation_left": 100}, {"repeated_header_rows": True},
    {"trailing_note_rows": -1}, {"date_mode": DateMode.PER_TRANSACTION},
])
def test_group_profile_validation(changes):
    profile = LayoutProfile(DateMode.GROUPED, AmountMode.GROUP_SUBTOTAL, BalanceMode.ABSENT,
                            balance_column=None, transaction_left=110, continuation_left=240)
    with pytest.raises(ValueError):
        replace(profile, **changes)


def test_wrapped_summary_distance_is_explicit(flow_pdf):
    document = extract_pdf(flow_pdf)
    assert infer_layout(document).status == InferenceStatus.SUCCESS
    assert infer_layout(document, tolerances=Tolerances(summary_y=0)).status == InferenceStatus.UNSUPPORTED


def test_same_capability_works_at_different_column_positions(changed_document):
    def change(pages):
        for page in pages:
            for row in page:
                for cell in row["cells"]:
                    cell[0] += 13
    document = changed_document(change)
    inference = infer_layout(document)
    assert inference.status == InferenceStatus.SUCCESS and inference.profile is not None
    assert abs(inference.profile.transaction_left - 123) < 1
    assert abs(inference.profile.continuation_left - 253) < 1
    assert len(GenericStatementParser().parse(document, inference.profile).transactions) == 4


def test_grouped_unsigned_flows_without_wrapping_do_not_require_a_continuation_column(changed_document):
    def change(pages):
        for page in pages:
            page[:] = [row for row in page if not (len(row["cells"]) == 1 and row["cells"][0][0] == 240)]
    document = changed_document(change)
    inference = infer_layout(document)
    assert inference.status == InferenceStatus.SUCCESS and inference.profile is not None
    assert inference.profile.continuation_left is None
    assert len(GenericStatementParser().parse(document, inference.profile).transactions) == 4


def test_duplicate_grouped_transactions_are_retained(changed_document):
    def change(pages):
        pages[0][12]["cells"][1][1] = "CLIENTE FICTÍCIO"
        pages[1][2]["cells"][0][1] = "REFERÊNCIA ALFA"
    document = changed_document(change)
    inference = infer_layout(document)
    assert inference.profile is not None
    statement = GenericStatementParser().parse(document, inference.profile)
    assert len(statement.transactions) == 4
    assert statement.transactions[0] == statement.transactions[1]


def test_missing_last_page_is_detected_even_when_totals_would_reconcile(changed_document):
    document = changed_document(lambda pages: pages.pop())
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


def test_explicit_group_profile_reports_empty_document_as_a_safe_parse_error():
    profile = LayoutProfile(DateMode.GROUPED, AmountMode.GROUP_SUBTOTAL, BalanceMode.ABSENT,
                            balance_column=None, transaction_left=110)
    with pytest.raises(StatementParseError, match="rows are required"):
        GenericStatementParser().parse(ExtractedDocument(()), profile)


def test_explicit_group_profile_reports_missing_monetary_area_as_a_safe_parse_error(make_document):
    document = make_document("Período: 01/04/2027 a 30/04/2027\nSaldo inicial: R$ 100,00\n"
                             "Saldo final: R$ 100,00\n01 ABR 2027\nOPERAÇÃO SEM VALOR")
    profile = LayoutProfile(DateMode.GROUPED, AmountMode.GROUP_SUBTOTAL, BalanceMode.ABSENT,
                            balance_column=None, transaction_left=20)
    with pytest.raises(StatementParseError, match="no monetary regions"):
        GenericStatementParser().parse(document, profile)
