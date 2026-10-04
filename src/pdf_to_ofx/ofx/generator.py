"""Deterministic OFX 1.02 with closed tags and ASCII character references.

Format choices remain provisional; Athenas compatibility is unverified.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

from pdf_to_ofx.domain.errors import OFXGenerationError
from pdf_to_ofx.domain.models import BankAccount, Statement, Transaction
from pdf_to_ofx.validation.statement import validate_statement

OFX_HEADER = (
    "OFXHEADER:100\n"
    "DATA:OFXSGML\n"
    "VERSION:102\n"
    "SECURITY:NONE\n"
    "ENCODING:USASCII\n"
    "CHARSET:NONE\n"
    "COMPRESSION:NONE\n"
    "OLDFILEUID:NONE\n"
    "NEWFILEUID:NONE\n\n"
)


@dataclass(frozen=True, slots=True)
class OFXProfile:
    # Explicitly fictitious, centralized metadata; never infer real account IDs.
    bank_id: str = "000"
    account_id: str = "SYNTHETIC-DEMO"
    account_type: str = "CHECKING"
    currency: str = "BRL"
    branch_id: str | None = None
    organization: str | None = None
    institution_id: str | None = None


def _account_profile(account: BankAccount) -> OFXProfile:
    return OFXProfile(
        bank_id=account.bank_id, account_id=account.account_id,
        account_type=account.account_type, branch_id=account.branch_id,
        organization=account.organization, institution_id=account.institution_id,
    )


def _resolve_profile(statement: Statement, profile: OFXProfile | None) -> OFXProfile:
    if statement.account is not None and not isinstance(statement.account, BankAccount):
        raise OFXGenerationError("Invalid statement account metadata.")
    if profile is None:
        if statement.account is not None:
            profile = _account_profile(statement.account)
        elif statement.bank_id == "synthetic":
            profile = OFXProfile()
        else:
            raise OFXGenerationError("Real-bank export requires explicit account metadata.")
    if statement.bank_id != "synthetic":
        if profile.bank_id == "000" or profile.account_id == "SYNTHETIC-DEMO":
            raise OFXGenerationError("Synthetic account metadata cannot be used for a real bank.")
        for value in (profile.branch_id, profile.organization, profile.institution_id):
            _check_text(value)
        if statement.account is not None and profile != _account_profile(statement.account):
            raise OFXGenerationError("Export profile contradicts the statement account metadata.")
    for value in (profile.bank_id, profile.account_id, profile.account_type, profile.currency):
        _check_text(value)
    if (profile.organization is None) != (profile.institution_id is None):
        raise OFXGenerationError("OFX institution name and ID must be provided together.")
    for value in (profile.branch_id, profile.organization, profile.institution_id):
        if value is not None:
            _check_text(value)
    return profile


def _check_text(value: str | None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OFXGenerationError("OFX text fields must be nonempty strings.")
    if any(
        not (ord(char) in (9, 10, 13) or 0x20 <= ord(char) <= 0xD7FF
             or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
        for char in value
    ):
        raise OFXGenerationError("OFX text contains an unsupported control or Unicode character.")


def _add(parent: ET.Element, tag: str, value: str) -> None:
    ET.SubElement(parent, tag).text = value


def _date(value: date) -> str:
    return f"{value.year:04d}{value.month:02d}{value.day:02d}000000"


def _identity(statement: Statement, transaction: Transaction, profile: OFXProfile) -> str:
    values = [statement.bank_id, statement.layout_id, profile.bank_id, profile.account_id,
              transaction.posting_date.isoformat(), transaction.description,
              format(transaction.amount, ".2f")]
    if profile.branch_id is not None:
        values.append(profile.branch_id)
    return json.dumps(
        values,
        ensure_ascii=True, separators=(",", ":"),
    )


def generate_ofx(statement: Statement, profile: OFXProfile | None = None) -> str:
    # Direct callers receive the same safety gate as the application pipeline.
    validate_statement(statement)
    profile = _resolve_profile(statement, profile)
    if profile.account_type not in {"CHECKING", "SAVINGS"}:
        raise OFXGenerationError("Unsupported OFX account type.")
    if (len(profile.currency) != 3 or not profile.currency.isascii()
            or not profile.currency.isalpha() or not profile.currency.isupper()):
        raise OFXGenerationError("OFX currency must be a three-letter uppercase code.")
    if statement.closing_balance is None:
        raise OFXGenerationError("The provisional OFX profile requires a closing balance.")
    for transaction in statement.transactions:
        _check_text(transaction.description)

    root = ET.Element("OFX")
    signon = ET.SubElement(ET.SubElement(root, "SIGNONMSGSRSV1"), "SONRS")
    status = ET.SubElement(signon, "STATUS")
    _add(status, "CODE", "0")
    _add(status, "SEVERITY", "INFO")
    # Statement end is the stable reference date for this provisional export.
    _add(signon, "DTSERVER", _date(statement.period_end))
    _add(signon, "LANGUAGE", "POR")
    if profile.organization is not None:
        assert profile.institution_id is not None  # Enforced by _resolve_profile.
        institution = ET.SubElement(signon, "FI")
        _add(institution, "ORG", profile.organization)
        _add(institution, "FID", profile.institution_id)
    response = ET.SubElement(ET.SubElement(root, "BANKMSGSRSV1"), "STMTTRNRS")
    _add(response, "TRNUID", "0")
    status = ET.SubElement(response, "STATUS")
    _add(status, "CODE", "0")
    _add(status, "SEVERITY", "INFO")
    bank_statement = ET.SubElement(response, "STMTRS")
    _add(bank_statement, "CURDEF", profile.currency)
    account = ET.SubElement(bank_statement, "BANKACCTFROM")
    _add(account, "BANKID", profile.bank_id)
    if profile.branch_id is not None:
        _add(account, "BRANCHID", profile.branch_id)
    _add(account, "ACCTID", profile.account_id)
    _add(account, "ACCTTYPE", profile.account_type)
    transaction_list = ET.SubElement(bank_statement, "BANKTRANLIST")
    _add(transaction_list, "DTSTART", _date(statement.period_start))
    _add(transaction_list, "DTEND", _date(statement.period_end))
    occurrences: Counter[str] = Counter()
    used_ids: set[str] = set()
    for transaction in statement.transactions:
        identity = _identity(statement, transaction, profile)
        occurrences[identity] += 1
        # Preserve identical repeated transactions with distinct stable FITIDs.
        payload = f"{identity}:{occurrences[identity]}".encode("ascii")
        fitid = hashlib.sha256(payload).hexdigest()
        if fitid in used_ids:
            raise OFXGenerationError("Duplicate OFX transaction identifier.")
        used_ids.add(fitid)
        element = ET.SubElement(transaction_list, "STMTTRN")
        _add(element, "TRNTYPE", "CREDIT" if transaction.amount >= 0 else "DEBIT")
        _add(element, "DTPOSTED", _date(transaction.posting_date))
        _add(element, "TRNAMT", format(transaction.amount, ".2f"))
        _add(element, "FITID", fitid)
        _add(element, "MEMO", transaction.description)
    ledger = ET.SubElement(bank_statement, "LEDGERBAL")
    _add(ledger, "BALAMT", format(statement.closing_balance, ".2f"))
    _add(ledger, "DTASOF", _date(statement.period_end))
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    # Match the declared ASCII encoding while retaining accents through entities.
    return OFX_HEADER + body.encode("ascii", "xmlcharrefreplace").decode("ascii") + "\n"
