from dataclasses import replace
from decimal import Decimal, localcontext
from xml.etree import ElementTree as ET

import pytest

from pdf_to_ofx.domain.errors import OFXGenerationError, StatementValidationError
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.ofx.generator import OFX_HEADER, OFXProfile, generate_ofx


def body(ofx: str) -> ET.Element:
    header, document = ofx.split("\n\n", maxsplit=1)
    assert header + "\n\n" == OFX_HEADER
    ofx.encode("ascii")  # Payload must match the declared encoding.
    return ET.fromstring(document)


def test_provisional_ofx_structure_and_financial_values(statement: Statement) -> None:
    root = body(generate_ofx(statement))
    assert root.tag == "OFX"
    assert root.findtext("./SIGNONMSGSRSV1/SONRS/STATUS/CODE") == "0"
    response = root.find("./BANKMSGSRSV1/STMTTRNRS/STMTRS")
    assert response is not None
    assert response.findtext("CURDEF") == "BRL"
    assert response.findtext("BANKACCTFROM/ACCTID") == "SYNTHETIC-DEMO"
    assert response.findtext("BANKACCTFROM/BANKID") == "000"
    assert response.findtext("BANKACCTFROM/ACCTTYPE") == "CHECKING"
    assert response.findtext("BANKTRANLIST/DTSTART") == "20260901000000"
    assert response.findtext("BANKTRANLIST/DTEND") == "20260905000000"
    transactions = response.findall("./BANKTRANLIST/STMTTRN")
    assert len(transactions) == 4
    assert [transaction.findtext("TRNAMT") for transaction in transactions] == [
        "500.00", "-35.00", "-200.00", "100.00",
    ]
    assert [transaction.findtext("TRNTYPE") for transaction in transactions] == [
        "CREDIT", "DEBIT", "DEBIT", "CREDIT",
    ]
    assert [transaction.findtext("DTPOSTED") for transaction in transactions] == [
        "20260901000000", "20260902000000", "20260903000000", "20260905000000",
    ]
    assert [transaction.findtext("MEMO") for transaction in transactions] == [
        transaction.description for transaction in statement.transactions
    ]
    ids = [transaction.findtext("FITID") for transaction in transactions]
    assert len(set(ids)) == 4
    assert all(identifier is not None and len(identifier) == 64 for identifier in ids)
    assert response.findtext("LEDGERBAL/BALAMT") == "1365.00"
    assert response.findtext("LEDGERBAL/DTASOF") == "20260905000000"


def test_export_and_fitids_are_deterministic(statement: Statement) -> None:
    assert generate_ofx(statement) == generate_ofx(statement)
    original_ids = [element.text for element in body(generate_ofx(statement)).findall(".//FITID")]
    equivalent = replace(statement, transactions=tuple(
        replace(transaction, amount=Decimal(format(transaction.amount, ".3f")))
        for transaction in statement.transactions
    ))
    assert [element.text for element in body(generate_ofx(equivalent)).findall(".//FITID")] == original_ids


def test_identical_transactions_are_preserved_with_unique_stable_ids(statement: Statement) -> None:
    duplicate = replace(statement, transactions=(statement.transactions[0], statement.transactions[0]),
                        closing_balance=Decimal("2000.00"))
    first = generate_ofx(duplicate)
    ids = [element.text for element in body(first).findall(".//FITID")]
    assert len(ids) == len(set(ids)) == 2
    assert first == generate_ofx(duplicate)


def test_description_is_escaped_without_losing_unicode(statement: Statement) -> None:
    description = "PAGAMENTO <DEMO> & FICÇÃO"
    first = replace(statement.transactions[0], description=description)
    exported = generate_ofx(replace(statement, transactions=(first, *statement.transactions[1:])))
    assert "&lt;DEMO&gt; &amp;" in exported
    assert body(exported).findtext(".//MEMO") == description


def test_direct_generation_rejects_invalid_financial_data(statement: Statement) -> None:
    with pytest.raises(StatementValidationError):
        generate_ofx(replace(statement, closing_balance=Decimal("1365.01")))


def test_provisional_profile_requires_closing_balance(statement: Statement) -> None:
    with pytest.raises(OFXGenerationError, match="closing balance"):
        generate_ofx(replace(statement, closing_balance=None))


@pytest.mark.parametrize("description", ["BAD\x00TEXT", "BAD\ud800TEXT"])
def test_invalid_export_characters_fail(statement: Statement, description: str) -> None:
    first = replace(statement.transactions[0], description=description)
    with pytest.raises(OFXGenerationError):
        generate_ofx(replace(statement, transactions=(first, *statement.transactions[1:])))


def test_account_metadata_is_configurable(statement: Statement) -> None:
    profile = OFXProfile(bank_id="999", account_id="OTHER-DEMO", account_type="SAVINGS")
    root = body(generate_ofx(statement, profile))
    assert root.findtext(".//BANKID") == "999"
    assert root.findtext(".//ACCTID") == "OTHER-DEMO"
    assert root.findtext(".//ACCTTYPE") == "SAVINGS"


@pytest.mark.parametrize("profile", [
    OFXProfile(bank_id=""), OFXProfile(account_id="BAD\x00ID"),
    OFXProfile(account_type="UNKNOWN"), OFXProfile(currency="B1R"),
    OFXProfile(currency="brl"),
])
def test_invalid_profile_fails(statement: Statement, profile: OFXProfile) -> None:
    with pytest.raises(OFXGenerationError):
        generate_ofx(statement, profile)


def test_low_decimal_precision_does_not_round_export(statement: Statement) -> None:
    with localcontext() as context:
        context.prec = 2
        assert [element.text for element in body(generate_ofx(statement)).findall(".//TRNAMT")] == [
            "500.00", "-35.00", "-200.00", "100.00",
        ]
