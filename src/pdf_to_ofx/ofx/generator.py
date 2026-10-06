"""Deterministic OFX 1.02 with closed tags and ASCII character references.

Format choices remain provisional; Athenas compatibility is unverified.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

from pdf_to_ofx.domain.errors import (
    MissingOFXMetadataError, MissingOFXRequirementsError, OFXGenerationError,
    OFXCurrencyConflictError, UnresolvedCurrencyError,
)
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
    # Identifiers have no defaults: callers must supply account identity.
    bank_id: str
    account_id: str
    account_type: str = "CHECKING"
    currency: str | None = None
    branch_id: str | None = None
    organization: str | None = None
    institution_id: str | None = None


_SYNTHETIC_PROFILE = OFXProfile(bank_id="000", account_id="SYNTHETIC-DEMO")


def account_profile(account: BankAccount) -> OFXProfile:
    return OFXProfile(
        bank_id=account.bank_id, account_id=account.account_id,
        account_type=account.account_type, branch_id=account.branch_id,
        organization=account.organization, institution_id=account.institution_id,
    )


def _resolve_profile(statement: Statement, profile: OFXProfile | None) -> OFXProfile:
    if statement.account is not None and not isinstance(statement.account, BankAccount):
        raise OFXGenerationError("Invalid statement account metadata.")
    # Generic interpretation must never gain demo defaults merely because an
    # external identity context contains the reserved synthetic bank marker.
    synthetic = statement.bank_id == "synthetic" and statement.layout_id == "synthetic-v1"
    if profile is None:
        if statement.account is not None:
            profile = account_profile(statement.account)
        elif synthetic:
            profile = _SYNTHETIC_PROFILE
        else:
            raise MissingOFXMetadataError("Real-bank export requires explicit account metadata.")
    if not isinstance(profile, OFXProfile):
        raise OFXGenerationError("Invalid OFX export profile.")
    for value in (profile.bank_id, profile.account_id, profile.account_type):
        if value is None or isinstance(value, str) and not value.strip():
            raise MissingOFXMetadataError("Required OFX profile metadata is absent.")
        _check_text(value)
    if profile.currency is not None:
        _check_text(profile.currency)
    real_bank = not synthetic
    for value in (profile.branch_id, profile.organization, profile.institution_id):
        if real_bank or value is not None:
            if value is None or isinstance(value, str) and not value.strip():
                raise MissingOFXMetadataError("Required OFX institution or account metadata is absent.")
            _check_text(value)
    if (profile.organization is None) != (profile.institution_id is None):
        raise OFXGenerationError("OFX institution name and ID must be provided together.")
    if real_bank:
        assert profile.organization is not None and profile.institution_id is not None
        if (profile.bank_id.strip() == _SYNTHETIC_PROFILE.bank_id
                or profile.account_id.strip() == _SYNTHETIC_PROFILE.account_id
                or profile.institution_id.strip() == "000"
                or profile.organization.strip().casefold() == "synthetic bank"):
            raise OFXGenerationError("Synthetic account metadata cannot be used for a real bank.")
        if statement.account is not None and replace(profile, currency=None) != account_profile(statement.account):
            raise OFXGenerationError("Export profile contradicts the statement account metadata.")
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
    # The domain contains calendar dates, not times or timezones.
    return f"{value.year:04d}{value.month:02d}{value.day:02d}"


def _money(value: Decimal) -> str:
    # Validation guarantees exact cents; signed zero has no financial identity.
    return "0.00" if value == 0 else format(value, ".2f")


def _identity(statement: Statement, transaction: Transaction, profile: OFXProfile) -> str:
    values = [statement.bank_id, statement.layout_id, profile.bank_id, profile.account_id,
              transaction.posting_date.isoformat(), transaction.description,
              _money(transaction.amount)]
    if profile.branch_id is not None:
        values.append(profile.branch_id)
    return json.dumps(
        values,
        ensure_ascii=True, separators=(",", ":"),
    )


def validate_ofx_export(statement: Statement, profile: OFXProfile | None = None) -> OFXProfile:
    """Assess actual generator requirements without creating an OFX payload."""
    validate_statement(statement)
    profile = _resolve_profile(statement, profile)
    if profile.account_type not in {"CHECKING", "SAVINGS"}:
        raise OFXGenerationError("Unsupported OFX account type.")
    if statement.currency is None:
        raise UnresolvedCurrencyError("Currency could not be resolved from statement contents.")
    if profile.currency is not None:
        if profile.currency != statement.currency.code:
            raise OFXCurrencyConflictError(
                f"OFX profile specifies '{profile.currency}' but statement contains evidence for '{statement.currency.code}'."
            )
        if (len(profile.currency) != 3 or not profile.currency.isascii()
                or not profile.currency.isalpha() or not profile.currency.isupper()):
            raise OFXGenerationError("OFX currency must be a three-letter uppercase code.")
    if statement.closing_balance is None:
        raise MissingOFXRequirementsError("The provisional OFX profile requires a closing balance.")
    for transaction in statement.transactions:
        _check_text(transaction.description)
    return profile


def generate_ofx(statement: Statement, profile: OFXProfile | None = None) -> str:
    # Direct callers receive the same safety gate as the application pipeline.
    profile = validate_ofx_export(statement, profile)

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
    assert statement.currency is not None
    _add(bank_statement, "CURDEF", statement.currency.code)
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
    # Validate declared economic order and available balances BEFORE sorting.
    # Same-day ties use description then signed amount, never incidental input
    # order. Identical exported transactions use stable occurrence numbers.
    transactions = sorted(statement.transactions, key=lambda transaction: (
        transaction.posting_date, transaction.description, transaction.amount,
    ))
    for transaction in transactions:
        identity = _identity(statement, transaction, profile)
        occurrences[identity] += 1
        # Preserve identical repeated transactions with distinct stable FITIDs.
        payload = f"{identity}:{occurrences[identity]}".encode("ascii")
        fitid = hashlib.sha256(payload).hexdigest()
        if fitid in used_ids:
            raise OFXGenerationError("Duplicate OFX transaction identifier.")
        used_ids.add(fitid)
        element = ET.SubElement(transaction_list, "STMTTRN")
        # Sign is all our generic model knows: a debit need not be a PAYMENT.
        # Zero is neutral and follows the nonnegative CREDIT convention.
        _add(element, "TRNTYPE", "CREDIT" if transaction.amount >= 0 else "DEBIT")
        _add(element, "DTPOSTED", _date(transaction.posting_date))
        _add(element, "TRNAMT", _money(transaction.amount))
        _add(element, "FITID", fitid)
        # Preserve the full source description once; no inferred payee (NAME),
        # check number (CHECKNUM), or reference number (REFNUM).
        _add(element, "MEMO", transaction.description)
    ledger = ET.SubElement(bank_statement, "LEDGERBAL")
    _add(ledger, "BALAMT", _money(statement.closing_balance))
    _add(ledger, "DTASOF", _date(statement.period_end))
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    # Match the declared ASCII encoding while retaining accents through entities.
    return OFX_HEADER + body.encode("ascii", "xmlcharrefreplace").decode("ascii") + "\n"
