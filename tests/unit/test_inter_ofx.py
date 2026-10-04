from dataclasses import replace
from decimal import Decimal
from xml.etree import ElementTree as ET
from unittest.mock import patch

import pytest

from pdf_to_ofx.domain.errors import OFXGenerationError, StatementValidationError
from pdf_to_ofx.domain.models import Statement
from pdf_to_ofx.ofx.generator import OFXProfile, generate_ofx


def root(statement: Statement, profile: OFXProfile | None = None) -> ET.Element:
    ofx = generate_ofx(statement, profile)
    ofx.encode("ascii")
    return ET.fromstring(ofx.split("\n\n", 1)[1])


def test_source_account_metadata_and_signed_values(inter_statement: Statement) -> None:
    document = root(inter_statement)
    assert document.findtext(".//FI/ORG") == "Banco Inter"
    assert document.findtext(".//FI/FID") == "077"
    assert document.findtext(".//BANKID") == "077"
    assert document.findtext(".//BRANCHID") == "9999-9"
    assert document.findtext(".//ACCTID") == "99999999"
    assert document.findtext(".//ACCTTYPE") == "CHECKING"
    assert document.findtext(".//CURDEF") == "BRL"
    assert [node.text for node in document.findall(".//TRNAMT")] == ["250.00", "-100.00", "50.00", "-70.00", "20.00"]
    assert [node.text for node in document.findall(".//TRNTYPE")] == ["CREDIT", "DEBIT", "CREDIT", "DEBIT", "CREDIT"]
    assert [node.text for node in document.findall(".//MEMO")] == [t.description for t in inter_statement.transactions]
    assert document.findtext(".//LEDGERBAL/BALAMT") == "1000.00"
    assert not document.findall(".//NAME")


def test_real_statement_without_metadata_fails(inter_statement: Statement) -> None:
    with pytest.raises(OFXGenerationError, match="metadata"):
        generate_ofx(replace(inter_statement, account=None))


def test_default_synthetic_profile_is_rejected_for_real_bank(inter_statement: Statement) -> None:
    with pytest.raises(OFXGenerationError, match="Synthetic"):
        generate_ofx(inter_statement, OFXProfile(
            bank_id="000", account_id="SYNTHETIC-DEMO", branch_id="9999-9",
            organization="Synthetic Bank", institution_id="000",
        ))


@pytest.mark.parametrize("changes", [
    {"branch_id": ""}, {"account_id": ""}, {"bank_id": "000"},
    {"institution_id": ""}, {"organization": ""}, {"account_type": "UNKNOWN"},
])
def test_missing_invalid_or_fictitious_metadata_fails(inter_statement: Statement, changes: dict) -> None:
    assert inter_statement.account is not None
    invalid = replace(inter_statement, account=replace(inter_statement.account, **changes))
    with pytest.raises(OFXGenerationError):
        generate_ofx(invalid)


def test_profile_conflict_is_not_silently_applied(inter_statement: Statement) -> None:
    profile = OFXProfile(bank_id="077", account_id="OTHER-FICTITIOUS", branch_id="9999-9",
                         organization="Banco Inter", institution_id="077")
    with pytest.raises(OFXGenerationError, match="contradicts"):
        generate_ofx(inter_statement, profile)


def test_explicit_metadata_is_supported(inter_statement: Statement) -> None:
    profile = OFXProfile(bank_id="077", account_id="99999999", branch_id="9999-9",
                         organization="Banco Inter", institution_id="077")
    assert root(replace(inter_statement, account=None), profile).findtext(".//ACCTID") == "99999999"


def test_branch_participates_in_fitid_identity(inter_statement: Statement) -> None:
    assert inter_statement.account is not None
    changed = replace(inter_statement, account=replace(inter_statement.account, branch_id="88888"))
    first = [node.text for node in root(inter_statement).findall(".//FITID")]
    other = [node.text for node in root(changed).findall(".//FITID")]
    assert first != other
    assert first == [node.text for node in root(inter_statement).findall(".//FITID")]


def test_duplicate_date_amount_description_keeps_unique_ids(inter_statement: Statement) -> None:
    first = inter_statement.transactions[0]
    second = replace(first, balance_after=Decimal("1350.00"))
    duplicate = replace(inter_statement, transactions=(first, second), closing_balance=Decimal("1350.00"))
    ids = [node.text for node in root(duplicate).findall(".//FITID")]
    assert len(ids) == len(set(ids)) == 2
    assert ids == [node.text for node in root(duplicate).findall(".//FITID")]


def test_direct_export_rejects_bad_running_balance(inter_statement: Statement) -> None:
    first = replace(inter_statement.transactions[0], balance_after=Decimal("1100.01"))
    invalid = replace(inter_statement, transactions=(first, *inter_statement.transactions[1:]))
    with pytest.raises(StatementValidationError):
        generate_ofx(invalid)


def test_identifier_collision_fails_explicitly(inter_statement: Statement) -> None:
    with patch("pdf_to_ofx.ofx.generator.hashlib.sha256") as digest:
        digest.return_value.hexdigest.return_value = "0" * 64
        with pytest.raises(OFXGenerationError, match="identifier"):
            generate_ofx(inter_statement)
