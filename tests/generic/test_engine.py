from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import convert_pdf, parse_pdf
from pdf_to_ofx.application.export import write_ofx
from pdf_to_ofx.banks.inter import InterParser
from pdf_to_ofx.domain.errors import OFXGenerationError, StatementParseError, StatementValidationError
from pdf_to_ofx.domain.models import BankAccount
from pdf_to_ofx.generic.inference import InferenceStatus, infer_layout
from pdf_to_ofx.generic.parser import GenericStatementParser, StatementContext
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode, LayoutProfile
from pdf_to_ofx.ofx.generator import generate_ofx
from pdf_to_ofx.pdf.document import ExtractedDocument, ExtractedPage
from pdf_to_ofx.pdf.extractor import extract_pdf

HEADER = "Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\nSaldo final: R$ 100,01\n"
BODY = "01/03/2027 CRÉDITO ALFA R$ 0,01 R$ 100,01\n02/03/2027 DÉBITO BETA -R$ 0,01 R$ 100,00\n02/03/2027 CRÉDITO GAMA R$ 0,01 R$ 100,01"


def test_structural_pdf_recipes_and_multipage_context(layout_pdf):
    document = extract_pdf(layout_pdf)
    inference = infer_layout(document)
    assert inference.status == InferenceStatus.SUCCESS and inference.profile is not None
    profile = LayoutProfile.from_json(inference.profile.to_json())
    assert profile.amount_mode == AmountMode.SIGNED
    assert profile.balance_mode == BalanceMode.RUNNING
    statement = GenericStatementParser().parse(document, profile)
    assert len(statement.transactions) == 3
    assert [t.posting_date for t in statement.transactions] == [date(2027, 3, 1), date(2027, 3, 2), date(2027, 3, 2)]
    assert [t.amount for t in statement.transactions] == [Decimal("0.01"), Decimal("-0.01"), Decimal("0.01")]
    assert [t.balance_after for t in statement.transactions] == [Decimal("100.01"), Decimal("100.00"), Decimal("100.01")]
    assert [t.description for t in statement.transactions] == ["CRÉDITO ALFA", "PAGAMENTO JOÃO", "TRANSFERÊNCIA BETA"]
    assert statement.period_start == date(2027, 3, 1) and statement.period_end == date(2027, 3, 3)
    assert statement.opening_balance == Decimal("100.00") and statement.closing_balance == Decimal("100.01")
    assert statement.bank_id == "unknown" and statement.account is None


def test_structural_fixture_generation_is_reproducible(layout_pdf, write_layout_pdf, tmp_path):
    # The parameterized fixture exposes its structural family via its header.
    document = extract_pdf(layout_pdf)
    profile = infer_layout(document).profile
    assert profile is not None
    name = "grouped_signed_balance" if profile.date_mode == DateMode.GROUPED else "per_row_signed_balance"
    other = tmp_path / "again.pdf"
    layouts = Path(__file__).parents[1] / "fixtures" / "layouts"
    write_layout_pdf(layouts / name / "pages.json", other)
    assert other.read_bytes() == layout_pdf.read_bytes()


def test_public_specific_and_generic_are_equivalent_using_only_identity_context(inter_pdf):
    document = extract_pdf(inter_pdf)
    specific = InterParser().parse(document)
    inference = infer_layout(document)
    assert inference.status == InferenceStatus.SUCCESS and inference.profile is not None
    assert inference.profile.date_mode == DateMode.GROUPED
    assert inference.profile.carry_date_across_pages
    assert inference.profile.footer_rows == 2
    # Account identity is external; periods, balances and transactions are NOT
    # passed from the baseline into generic interpretation.
    context = StatementContext(bank_id=specific.bank_id, account=specific.account)
    generic = GenericStatementParser().parse(document, inference.profile, context=context)
    assert replace(generic, layout_id=specific.layout_id) == specific
    assert generate_ofx(generic) == generate_ofx(generic)


def test_generic_uses_positioned_words_instead_of_text_bank_detection(inter_pdf):
    document = extract_pdf(inter_pdf)
    profile = infer_layout(document).profile
    assert profile is not None
    without_text = replace(document, pages=tuple(replace(page, text="UNREGISTERED INSTITUTION") for page in document.pages))
    with patch("pdf_to_ofx.banks.inter.InterParser.parse", side_effect=AssertionError("Specific parser forbidden")):
        generic = GenericStatementParser().parse(without_text, profile)
    assert len(generic.transactions) == 5 and generic.bank_id == "unknown"


def test_two_mathematically_valid_column_assignments_are_ambiguous(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\n01/03/2027 ALFA R$ 0,01 -R$ 0,01\n02/03/2027 BETA R$ 0,02 R$ 0,01")
    result = infer_layout(document)
    assert result.status == InferenceStatus.AMBIGUOUS and result.profile is None


def test_math_identifies_movement_even_when_it_is_the_second_region(make_document):
    text = HEADER + "01/03/2027 ALFA R$ 100,01 R$ 0,01\n02/03/2027 BETA R$ 100,00 -R$ 0,01\n02/03/2027 GAMA R$ 100,01 R$ 0,01"
    result = infer_layout(make_document(text))
    assert result.status == InferenceStatus.SUCCESS and result.profile is not None
    assert result.profile.movement_column == 1 and result.profile.balance_column == 0


@pytest.mark.parametrize("document", [ExtractedDocument(()), ExtractedDocument((ExtractedPage(1, "TEXT ONLY"),))])
def test_insufficient_structure_is_unsupported(document):
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


def test_one_row_without_arithmetic_evidence_is_not_inferred(make_document):
    document = make_document("Período: 01/03/2027 a 03/03/2027\nSaldo final: R$ 100,01\n01/03/2027 ALFA R$ 0,01 R$ 100,01")
    assert infer_layout(document).status == InferenceStatus.AMBIGUOUS


@pytest.mark.parametrize(("old", "new"), [
    ("-R$ 0,01", "-R$ BAD"), ("-R$ 0,01", "-R$ 0,02"),
    ("R$ 100,00\n", "R$ 100,02\n"), ("Saldo final: R$ 100,01", "Saldo final: R$ 100,02"),
    ("DÉBITO BETA -R$", "DÉBITO BETA R$ 2,00 -R$"),
    ("02/03/2027 DÉBITO", "31/02/2027 DÉBITO"),
])
def test_invalid_financial_rows_never_form_a_valid_inference(make_document, old, new):
    expected = InferenceStatus.INVALID if new in {"-R$ 0,02", "R$ 100,02\n", "Saldo final: R$ 100,02"} else InferenceStatus.UNSUPPORTED
    assert infer_layout(make_document((HEADER + BODY).replace(old, new))).status == expected


def test_explicit_profile_also_rejects_financial_inconsistency(make_document):
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    with pytest.raises(StatementValidationError, match="Running balance"):
        GenericStatementParser().parse(make_document((HEADER + BODY).replace("-R$ 0,01", "-R$ 0,02")), profile)


def test_group_context_can_be_disabled_explicitly(make_document):
    document = make_document(HEADER + "01 de março de 2027\nALFA R$ 0,01 R$ 100,01", "BETA -R$ 0,01 R$ 100,00\nGAMA R$ 0,01 R$ 100,01")
    profile = LayoutProfile(DateMode.GROUPED, AmountMode.SIGNED, BalanceMode.RUNNING)
    assert len(GenericStatementParser().parse(document, profile).transactions) == 3
    with pytest.raises(StatementParseError, match="date context"):
        GenericStatementParser().parse(document, replace(profile, carry_date_across_pages=False))


def test_credit_debit_markers_are_a_generic_capability(make_document):
    text = (HEADER + BODY).replace("-R$ 0,01", "0,01 D").replace("R$ 0,01", "0,01 C")
    result = infer_layout(make_document(text))
    assert result.status == InferenceStatus.SUCCESS and result.profile is not None
    assert result.profile.amount_mode == AmountMode.CREDIT_DEBIT_MARKER


def test_absent_balance_requires_full_reconciliation_for_inference(make_document):
    document = make_document(HEADER + "01/03/2027 ALFA R$ 0,02\n02/03/2027 BETA -R$ 0,01")
    result = infer_layout(document)
    assert result.status == InferenceStatus.SUCCESS and result.profile is not None
    assert result.profile.balance_mode == BalanceMode.ABSENT
    without_opening = make_document((HEADER + "01/03/2027 ALFA R$ 0,02\n02/03/2027 BETA -R$ 0,01").replace("Saldo inicial: R$ 100,00\n", ""))
    assert infer_layout(without_opening).status == InferenceStatus.AMBIGUOUS


def test_duplicate_descriptions_and_amounts_are_preserved(make_document):
    text = "Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 0,00\nSaldo final: R$ 0,02\n01/03/2027 ALFA R$ 0,01 R$ 0,01\n01/03/2027 ALFA R$ 0,01 R$ 0,02"
    document = make_document(text)
    result = infer_layout(document)
    assert result.profile is not None
    statement = GenericStatementParser().parse(document, result.profile)
    assert len(statement.transactions) == 2
    assert statement.transactions[0].description == statement.transactions[1].description
    assert statement.transactions[0].amount == statement.transactions[1].amount == Decimal("0.01")


@pytest.mark.parametrize("extra", ["Saldo final: R$ 100,01 R$ BAD", "Saldo final: R$ 100,02", "OPERAÇÃO MALFORMADA R$ BAD"])
def test_unclassified_or_conflicting_header_finances_fail(make_document, extra):
    assert infer_layout(make_document(HEADER + extra + "\n" + BODY)).status == InferenceStatus.UNSUPPORTED


def test_arbitrary_text_rows_cannot_disappear_as_footers(make_document):
    text = HEADER + BODY + "\nOPERAÇÃO SEM VALOR\nSAC: FICTÍCIO Ouvidoria: FICTÍCIO"
    assert infer_layout(make_document(text)).status == InferenceStatus.UNSUPPORTED


def test_generic_contact_roles_are_not_institution_signatures(make_document):
    text = HEADER + BODY + "\nContato\nTelefone: FICTÍCIO Atendimento: FICTÍCIO"
    result = infer_layout(make_document(text))
    assert result.status == InferenceStatus.SUCCESS and result.profile is not None
    assert result.profile.footer_rows == 2


def test_explicit_footer_cannot_hide_financial_rows(make_document):
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING, footer_rows=1)
    with pytest.raises(StatementParseError, match="footer"):
        GenericStatementParser().parse(make_document(HEADER + BODY), profile)


def test_generic_parser_never_derives_missing_period_or_closing_balance(make_document):
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    with pytest.raises(StatementParseError, match="explicit statement period"):
        GenericStatementParser().parse(make_document(BODY), profile)
    without_closing = make_document((HEADER + BODY).replace("Saldo final: R$ 100,01\n", ""))
    statement = GenericStatementParser().parse(without_closing, profile)
    assert statement.closing_balance is None


def test_supplied_context_cannot_override_declared_values(make_document):
    profile = LayoutProfile(DateMode.PER_TRANSACTION, AmountMode.SIGNED, BalanceMode.RUNNING)
    with pytest.raises(StatementValidationError, match="Conflicting"):
        GenericStatementParser().parse(make_document(HEADER + BODY), profile,
                                      context=StatementContext(closing_balance=Decimal("200.00")))


def test_decimal_context_cannot_change_inference(make_document):
    with localcontext() as context:
        context.prec = 2
        assert infer_layout(make_document(HEADER + BODY)).status == InferenceStatus.SUCCESS


def test_unknown_bank_is_parsed_without_inventing_ofx_metadata(layout_pdf):
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
        result = parse_pdf(layout_pdf)
    assert result.layout_profile is not None
    assert result.statement.account is None and result.statement.bank_id == "unknown"
    assert len(result.statement.transactions) == 3
    with pytest.raises(OFXGenerationError, match="metadata"):
        convert_pdf(layout_pdf)


def test_unknown_bank_with_explicit_identity_exports_deterministic_ofx(layout_pdf, tmp_path):
    account = BankAccount("Banco Fictício", "999", "999", "0001", "00000001")
    context = StatementContext(bank_id="fictitious", account=account)
    first = convert_pdf(layout_pdf, context=context)
    second = convert_pdf(layout_pdf, context=context)
    assert first.ofx == second.ofx
    output = tmp_path / "generic.ofx"
    write_ofx(output, first.ofx)
    assert output.read_bytes() == second.ofx.encode("ascii")
    assert first.bank_name == "Banco Fictício"


def test_explicit_structural_path_never_calls_specific_parser(inter_pdf):
    profile = infer_layout(extract_pdf(inter_pdf)).profile
    assert profile is not None
    with patch("pdf_to_ofx.application.convert.detect_parser", side_effect=AssertionError("Detection forbidden")):
        result = parse_pdf(inter_pdf, layout_profile=profile)
    assert len(result.statement.transactions) == 5


def test_failure_of_a_recognized_specific_parser_is_not_bypassed(inter_pdf):
    with patch("pdf_to_ofx.banks.inter.InterParser.parse", side_effect=StatementParseError("Safe failure")), \
         patch("pdf_to_ofx.generic.parser.GenericStatementParser.parse") as generic:
        with pytest.raises(StatementParseError):
            parse_pdf(inter_pdf)
    generic.assert_not_called()


def test_generic_result_cannot_receive_synthetic_defaults_through_identity_context(layout_pdf):
    parsed = parse_pdf(layout_pdf, context=StatementContext(bank_id="synthetic"))
    assert parsed.statement.account is None
    with pytest.raises(OFXGenerationError, match="metadata"):
        generate_ofx(parsed.statement)


def test_dated_balance_heading_cannot_become_a_phantom_transaction(make_document):
    document = make_document((HEADER + BODY).replace("CRÉDITO ALFA", "Saldo do dia:"))
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


@pytest.mark.parametrize("where", ["movement", "closing"])
def test_orphan_negative_sign_cannot_be_absorbed_as_description_or_header(make_document, where):
    text = HEADER + BODY
    if where == "movement":
        text = text.replace("CRÉDITO ALFA R$", "CRÉDITO ALFA - R$", 1)
    else:
        text = text.replace("Saldo final: R$", "Saldo final: - R$", 1)
    document = make_document(text)
    page = document.pages[0]
    sign = next(word for word in page.words if word.text == "-")
    words = tuple(replace(word, x0=word.x0 + 20, x1=word.x1 + 20)
                  if word.top == sign.top and word.x0 > sign.x1 else word for word in page.words)
    document = replace(document, pages=(replace(page, words=words),))
    assert infer_layout(document).status == InferenceStatus.UNSUPPORTED


@pytest.mark.parametrize("currency", ["US$", "€", "r$"])
def test_unparsed_currency_cannot_be_silently_omitted_in_balances(make_document, currency):
    text = (HEADER + BODY).replace("Saldo final: R$", "Saldo final: " + currency)
    assert infer_layout(make_document(text)).status == InferenceStatus.UNSUPPORTED


@pytest.mark.parametrize("fields", [
    {"opening_balance": 100.0}, {"opening_balance": Decimal("NaN")},
    {"closing_balance": Decimal("Infinity")}, {"account": "INVALID"},
    {"period_start": "01/03/2027"}, {"bank_id": 123},
])
def test_context_is_validated_before_document_declarations_can_replace_invalid_input(fields):
    with pytest.raises(StatementValidationError):
        StatementContext(**fields)
