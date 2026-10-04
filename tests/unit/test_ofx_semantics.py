"""OFX semantics from public, fictitious domain data; no private fixtures."""

from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pdf_to_ofx.cli import _write_output
from pdf_to_ofx.domain.errors import OFXGenerationError, StatementValidationError
from pdf_to_ofx.domain.models import Statement, Transaction
from pdf_to_ofx.ofx.generator import OFXProfile, generate_ofx

REFERENCE = Path(__file__).parents[1] / "fixtures" / "ofx" / "reference.ofx"


def parse(ofx: str | bytes) -> ET.Element:
    data = ofx.encode("ascii") if isinstance(ofx, str) else ofx
    header, document = data.decode("ascii", errors="strict").split("\n\n", 1)
    assert dict(line.split(":", 1) for line in header.splitlines()) == {
        "OFXHEADER": "100", "DATA": "OFXSGML", "VERSION": "102",
        "SECURITY": "NONE", "ENCODING": "USASCII", "CHARSET": "NONE",
        "COMPRESSION": "NONE", "OLDFILEUID": "NONE", "NEWFILEUID": "NONE",
    }
    return ET.fromstring(document)


def structure(element: ET.Element) -> tuple:
    # Ignore indentation of containers, preserving leaf text and hierarchy.
    return (element.tag, element.text if len(element) == 0 else None,
            tuple(structure(child) for child in element))


def test_public_reference_and_complete_statement_semantics(ofx_reference_statement: Statement) -> None:
    statement = ofx_reference_statement
    document = parse(generate_ofx(statement))
    assert structure(document) == structure(parse(REFERENCE.read_bytes()))
    assert document.findtext(".//FI/ORG") == "Banco Fictício"
    assert document.findtext(".//FI/FID") == "999"
    assert document.findtext(".//BANKID") == "999"
    assert document.findtext(".//BRANCHID") == "0001"
    assert document.findtext(".//ACCTID") == "00000001"
    assert document.findtext(".//ACCTTYPE") == "CHECKING"
    assert document.findtext(".//CURDEF") == "BRL"
    assert document.findtext(".//BANKTRANLIST/DTSTART") == "20260901"
    assert document.findtext(".//BANKTRANLIST/DTEND") == "20260903"
    assert document.findtext(".//LEDGERBAL/DTASOF") == "20260903"
    assert document.findtext(".//DTSERVER") == "20260903"
    assert Decimal(document.findtext(".//LEDGERBAL/BALAMT")) == statement.closing_balance
    rows = document.findall(".//STMTTRN")
    # Multisets check multiplicity: omissions and extra transactions both fail.
    assert len(rows) == len(statement.transactions) == 3
    assert Counter(
        (date.fromisoformat(row.findtext("DTPOSTED")),
         Decimal(row.findtext("TRNAMT")), row.findtext("MEMO")) for row in rows
    ) == Counter((t.posting_date, t.amount, t.description) for t in statement.transactions)
    assert [row.findtext("MEMO") for row in rows] == ["CRÉDITO", "PAGAMENTO JOÃO", "TRANSFERÊNCIA"]
    assert [row.findtext("TRNAMT") for row in rows] == ["0.01", "-0.01", "0.01"]
    assert [row.findtext("TRNTYPE") for row in rows] == ["CREDIT", "DEBIT", "CREDIT"]
    assert len({row.findtext("FITID") for row in rows}) == 3
    for tag in ("DTSTART", "DTEND", "DTPOSTED", "DTASOF", "DTSERVER"):
        assert all(node.text is not None and len(node.text) == 8 and node.text.isdigit()
                   for node in document.findall(f".//{tag}"))
    for tag in ("NAME", "CHECKNUM", "REFNUM"):
        assert not document.findall(f".//{tag}")


@pytest.mark.parametrize("description", ["CRÉDITO", "TRANSFERÊNCIA", "PAGAMENTO JOÃO"])
def test_declared_encoding_matches_written_bytes_and_preserves_accents(
    statement: Statement, description: str, tmp_path: Path,
) -> None:
    transaction = replace(statement.transactions[0], description=description)
    source = replace(statement, transactions=(transaction, *statement.transactions[1:]))
    first, second = generate_ofx(source), generate_ofx(source)
    output = tmp_path / "accents.ofx"
    _write_output(output, first)
    data = output.read_bytes()
    assert data == first.encode("ascii") == second.encode("ascii")
    assert b"&#" in data  # Accents use valid numeric references, not misdeclared UTF-8.
    assert parse(data).findtext(".//MEMO") == description


def test_same_day_order_duplicates_and_ids_are_independent_of_input_order(statement: Statement) -> None:
    transactions = (
        Transaction(date(2026, 9, 1), "TRANSFERÊNCIA", Decimal("0.01")),
        Transaction(date(2026, 9, 1), "PAGAMENTO JOÃO", Decimal("0.01")),
        Transaction(date(2026, 9, 1), "TRANSFERÊNCIA", Decimal("-0.01")),
        Transaction(date(2026, 9, 1), "TRANSFERÊNCIA", Decimal("0.01")),
    )
    source = replace(statement, transactions=transactions,
                     opening_balance=Decimal("0.00"), closing_balance=Decimal("0.02"))
    expected = generate_ofx(source)
    for ordering in permutations(transactions):
        assert generate_ofx(replace(source, transactions=ordering)) == expected
    rows = parse(expected).findall(".//STMTTRN")
    assert [(row.findtext("MEMO"), row.findtext("TRNAMT")) for row in rows] == [
        ("PAGAMENTO JOÃO", "0.01"), ("TRANSFERÊNCIA", "-0.01"),
        ("TRANSFERÊNCIA", "0.01"), ("TRANSFERÊNCIA", "0.01"),
    ]
    assert len(rows) == len({row.findtext("FITID") for row in rows}) == 4
    # Sorting for export must not mutate the normalized statement.
    assert source.transactions == transactions


def test_source_running_balances_are_validated_before_export_sort(inter_statement: Statement) -> None:
    first, second, *others = inter_statement.transactions
    # Deliberately reverse the lexical description order of valid source rows.
    source = replace(inter_statement, transactions=(
        replace(first, description="Z FICTÍCIO"), replace(second, description="A FICTÍCIO"), *others,
    ))
    rows = parse(generate_ofx(source)).findall(".//STMTTRN")
    assert [row.findtext("MEMO") for row in rows[:2]] == ["A FICTÍCIO", "Z FICTÍCIO"]
    assert [row.findtext("TRNAMT") for row in rows[:2]] == ["-100.00", "250.00"]
    invalid = replace(source, transactions=(source.transactions[1], source.transactions[0], *others))
    with pytest.raises(StatementValidationError, match="Running balance"):
        generate_ofx(invalid)


def test_invalid_source_chronology_is_not_hidden_by_export_sort(statement: Statement) -> None:
    with pytest.raises(StatementValidationError, match="order"):
        generate_ofx(replace(statement, transactions=statement.transactions[::-1]))


def test_fitids_do_not_depend_on_period_balance_or_other_transactions(statement: Statement) -> None:
    original = parse(generate_ofx(statement)).findtext(".//FITID")
    isolated = replace(statement, period_end=date(2026, 9, 30), opening_balance=None,
                       closing_balance=Decimal("999.00"), transactions=(statement.transactions[0],))
    assert parse(generate_ofx(isolated)).findtext(".//FITID") == original


@pytest.mark.parametrize("field", ["posting_date", "description", "amount"])
def test_different_transaction_identity_changes_fitid(statement: Statement, field: str) -> None:
    first = statement.transactions[0]
    original = replace(statement, transactions=(first,), opening_balance=None)
    changes = {"posting_date": date(2026, 9, 2), "description": "OTHER FICTITIOUS", "amount": Decimal("0.01")}
    changed = replace(original, transactions=(replace(first, **{field: changes[field]}),))
    assert parse(generate_ofx(original)).findtext(".//FITID") != parse(generate_ofx(changed)).findtext(".//FITID")


@pytest.mark.parametrize("field", ["bank_id", "branch_id", "account_id"])
def test_different_account_identity_changes_fitid(ofx_reference_statement: Statement, field: str) -> None:
    source = ofx_reference_statement
    assert source.account is not None
    changed = replace(source, account=replace(source.account, **{field: "88888"}))
    assert parse(generate_ofx(source)).findtext(".//FITID") != parse(generate_ofx(changed)).findtext(".//FITID")


def test_signed_zero_is_canonical_and_uses_nonnegative_type(statement: Statement) -> None:
    transaction = replace(statement.transactions[0], amount=Decimal("0.00"))
    source = replace(statement, transactions=(transaction,),
                     opening_balance=Decimal("-0.00"), closing_balance=Decimal("-0.00"))
    first = generate_ofx(source)
    negative_zero = replace(source, transactions=(replace(transaction, amount=Decimal("-0.000")),))
    assert generate_ofx(negative_zero) == first
    root = parse(first)
    assert root.findtext(".//TRNAMT") == root.findtext(".//LEDGERBAL/BALAMT") == "0.00"
    assert root.findtext(".//TRNTYPE") == "CREDIT"


@pytest.mark.parametrize("field", ["bank_id", "account_id"])
def test_profile_identifiers_have_no_defaults(field: str) -> None:
    arguments = {"bank_id": "999", "account_id": "FICTITIOUS"}
    del arguments[field]
    with pytest.raises(TypeError):
        OFXProfile(**arguments)


@pytest.mark.parametrize("field", [
    "bank_id", "branch_id", "account_id", "organization", "institution_id", "account_type",
])
def test_real_account_required_metadata_cannot_be_absent(inter_statement: Statement, field: str) -> None:
    assert inter_statement.account is not None
    source = replace(inter_statement, account=replace(inter_statement.account, **{field: None}))
    with pytest.raises(OFXGenerationError):
        generate_ofx(source)


@pytest.mark.parametrize("changes", [
    {"organization": "Synthetic Bank"}, {"organization": " synthetic BANK "},
    {"institution_id": "000"}, {"bank_id": " 000 "}, {"account_id": " SYNTHETIC-DEMO "},
])
def test_real_account_rejects_known_synthetic_metadata(inter_statement: Statement, changes: dict) -> None:
    assert inter_statement.account is not None
    source = replace(inter_statement, account=replace(inter_statement.account, **changes))
    with pytest.raises(OFXGenerationError, match="Synthetic"):
        generate_ofx(source)


def test_invalid_profile_object_fails_explicitly(statement: Statement) -> None:
    with pytest.raises(OFXGenerationError, match="profile"):
        generate_ofx(statement, profile="invalid")
