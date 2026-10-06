import pytest
from xml.etree import ElementTree as ET
from decimal import Decimal
from dataclasses import replace

from unittest.mock import patch

from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.application.convert import assess_export_readiness, ExportStatus, StatementAnalysis
from pdf_to_ofx.domain.evidence import AnalysisStatus
from tests.generic.test_operators import positioned
from pdf_to_ofx.domain.currency import Currency
from pdf_to_ofx.domain.errors import CurrencyConflictError, UnresolvedCurrencyError, OFXCurrencyConflictError
from pdf_to_ofx.domain.models import BankAccount
from pdf_to_ofx.generic.parser import StatementContext
from pdf_to_ofx.generic.semantics import money_regions
from pdf_to_ofx.generic.structure import Tolerances, reconstruct_rows
from pdf_to_ofx.ofx.generator import generate_ofx, OFXProfile

def test_lexical_currency_observation(make_document):
    doc1 = make_document("R$ 1.234,56")
    row1 = reconstruct_rows(doc1)[0]
    regions1 = money_regions(row1, Tolerances())
    assert len(regions1) == 1
    assert regions1[0].money.currency == Currency("BRL")

    doc2 = make_document("1.234,56")
    row2 = reconstruct_rows(doc2)[0]
    regions2 = money_regions(row2, Tolerances())
    assert len(regions2) == 1
    assert regions2[0].money.currency is None


def test_statement_currency_resolution():
    # Pure BRL evidence
    header_brl = [[(20, "EXTRATO DE CONTA CORRENTE")], [(20, "Período: 01/03/2027 a 03/03/2027")],
                  [(20, "SALDO ANTERIOR R$ 100,00")], [(20, "SALDO FINAL R$ 100,01")]]
    body_brl = [
        [(20, "01/03/2027"), (100, "CRÉDITO ALFA"), (400, "R$ 0,01"), (500, "R$ 100,01")],
        [(20, "02/03/2027"), (100, "PAGAMENTO JOÃO"), (400, "-R$ 0,01"), (500, "R$ 100,00")],
        [(20, "02/03/2027"), (100, "TRANSFERÊNCIA BETA"), (400, "R$ 0,01"), (500, "R$ 100,01")]
    ]
    pdf = positioned(header_brl + body_brl)
    inference = infer_layout(pdf)
    assert inference.interpretation is not None, inference.reason
    assert inference.interpretation.statement.currency == Currency("BRL")

    # Mixed values (with and without symbol)
    header_mixed = [[(20, "EXTRATO DE CONTA CORRENTE")], [(20, "Período: 01/03/2027 a 03/03/2027")],
                  [(20, "SALDO ANTERIOR 100,00")], [(20, "SALDO FINAL 100,01")]]
    body_mixed = [
        [(20, "01/03/2027"), (100, "CRÉDITO ALFA"), (400, "R$ 0,01"), (500, "100,01")],
        [(20, "02/03/2027"), (100, "PAGAMENTO JOÃO"), (400, "-0,01"), (500, "100,00")],
        [(20, "02/03/2027"), (100, "TRANSFERÊNCIA BETA"), (400, "0,01"), (500, "100,01")]
    ]
    pdf_mixed = positioned(header_mixed + body_mixed)
    inference_mixed = infer_layout(pdf_mixed)
    assert inference_mixed.interpretation is not None, inference_mixed.reason
    assert inference_mixed.interpretation.statement.currency == Currency("BRL")

    # No currency evidence
    header_none = [[(20, "EXTRATO DE CONTA CORRENTE")], [(20, "Período: 01/03/2027 a 03/03/2027")],
                  [(20, "SALDO ANTERIOR 100,00")], [(20, "SALDO FINAL 100,01")]]
    body_none = [
        [(20, "01/03/2027"), (100, "CRÉDITO ALFA"), (400, "0,01"), (500, "100,01")],
        [(20, "02/03/2027"), (100, "PAGAMENTO JOÃO"), (400, "-0,01"), (500, "100,00")],
        [(20, "02/03/2027"), (100, "TRANSFERÊNCIA BETA"), (400, "0,01"), (500, "100,01")]
    ]
    pdf_none = positioned(header_none + body_none)
    inference_none = infer_layout(pdf_none)
    assert inference_none.interpretation is not None, inference_none.reason
    assert inference_none.interpretation.statement.currency is None

    # Exporting statement without currency is blocked
    with patch("pdf_to_ofx.application.convert._approved_statement", return_value=inference_none.interpretation.statement):
        analysis = StatementAnalysis(AnalysisStatus.SUCCESS, statement=inference_none.interpretation.statement)
        profile_mock = OFXProfile(bank_id="999", account_id="123", organization="A", institution_id="1", branch_id="1")
        readiness = assess_export_readiness(analysis, metadata=profile_mock)
    assert readiness.status == ExportStatus.EXPORT_REQUIREMENTS_MISSING
    assert "currency_unresolved" in readiness.issues


def test_explicit_declaration_and_conflicts():
    header_brl = [[(20, "EXTRATO DE CONTA CORRENTE")], [(20, "Período: 01/03/2027 a 03/03/2027")],
                  [(20, "SALDO ANTERIOR R$ 100,00")], [(20, "SALDO FINAL R$ 100,01")]]
    body_brl = [
        [(20, "01/03/2027"), (100, "CRÉDITO ALFA"), (400, "R$ 0,01"), (500, "R$ 100,01")],
        [(20, "02/03/2027"), (100, "PAGAMENTO JOÃO"), (400, "-R$ 0,01"), (500, "R$ 100,00")],
        [(20, "02/03/2027"), (100, "TRANSFERÊNCIA BETA"), (400, "R$ 0,01"), (500, "R$ 100,01")]
    ]
    pdf = positioned(header_brl + body_brl)
    
    # Compatible declaration
    inference_compat = infer_layout(pdf, context=StatementContext(currency=Currency("BRL")))
    assert inference_compat.interpretation.statement.currency == Currency("BRL")

    # Conflicting declaration
    inference_conflict = infer_layout(pdf, context=StatementContext(currency=Currency("USD")))
    # Failed closed during analysis due to conflict
    assert inference_conflict.status.name == "INVALID"
    assert "currency_conflict" in inference_conflict.blocking_capabilities


def test_ofx_profile_currency_interactions():
    header_brl = [[(20, "EXTRATO DE CONTA CORRENTE")], [(20, "Período: 01/03/2027 a 03/03/2027")],
                  [(20, "SALDO ANTERIOR R$ 100,00")], [(20, "SALDO FINAL R$ 100,01")]]
    body_brl = [
        [(20, "01/03/2027"), (100, "CRÉDITO ALFA"), (400, "R$ 0,01"), (500, "R$ 100,01")],
        [(20, "02/03/2027"), (100, "PAGAMENTO JOÃO"), (400, "-R$ 0,01"), (500, "R$ 100,00")],
        [(20, "02/03/2027"), (100, "TRANSFERÊNCIA BETA"), (400, "R$ 0,01"), (500, "R$ 100,01")]
    ]
    pdf = positioned(header_brl + body_brl)
    inference = infer_layout(pdf)
    assert inference.interpretation is not None, inference.reason
    statement = inference.interpretation.statement
    assert statement.currency == Currency("BRL")

    # Profile without currency shouldn't prevent exporting when statement has currency
    profile_none = OFXProfile(bank_id="999", account_id="123", currency=None, organization="A", institution_id="1", branch_id="1")
    ofx_none = generate_ofx(statement, profile_none)
    
    # <CURDEF> must derive from Statement.currency
    assert "<CURDEF>BRL" in ofx_none

    # Conflicting profile
    profile_conflict = OFXProfile(bank_id="999", account_id="123", currency="USD", organization="A", institution_id="1", branch_id="1")
    with pytest.raises(OFXCurrencyConflictError):
        generate_ofx(statement, profile_conflict)

    # Real bank statement with account metadata explicitly matched, plus currency in OFXProfile
    account_metadata = BankAccount(bank_id="999", account_id="123", account_type="CHECKING", branch_id="1", organization="A", institution_id="1")
    statement_with_account = replace(statement, account=account_metadata)
    profile_explicit = OFXProfile(bank_id="999", account_id="123", currency="BRL", organization="A", institution_id="1", branch_id="1")
    
    # Should be accepted without "contradicts the statement account metadata" error
    ofx_explicit = generate_ofx(statement_with_account, profile_explicit)
    assert "<CURDEF>BRL" in ofx_explicit
