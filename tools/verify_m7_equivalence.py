"""Read two local fixtures and report M7/M8 equivalence using aggregates only.

Usage: python tools/verify_m7_equivalence.py /local/first.pdf /local/second.pdf
Nothing is uploaded or written. The third document is deliberately excluded.
"""

import argparse
from dataclasses import replace
from pathlib import Path

from pdf_to_ofx.banks.inter import InterParser
from pdf_to_ofx.domain.errors import ConversionError
from pdf_to_ofx.domain.evidence import AnalysisStatus, EvidenceStatus
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.generic.semantics import parse_date, parse_money
from pdf_to_ofx.ofx.generator import OFXProfile, generate_ofx
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.pdf.sources import source_tokens


def verify(path: Path, expected: int, *, specific: bool) -> dict[str, int | bool]:
    document = extract_pdf(path)
    legacy, operators, composed = (infer_layout(document, **mode) for mode in
        ({"legacy": True}, {"operators": True}, {}))
    if any(r.status != AnalysisStatus.SUCCESS for r in (legacy, operators, composed)):
        raise ValueError("A fixture did not produce a successful interpretation.")
    old, new = legacy.interpretation, composed.interpretation
    if (len(new.statement.transactions) != expected or old.statement != new.statement
            or old.evidence != new.evidence or operators.interpretation.statement != new.statement
            or operators.interpretation.evidence != new.evidence):
        raise ValueError("Transaction counts, financial models or evidence differ.")
    if specific:
        baseline = InterParser().interpret(document)
        normalized = replace(new.statement, bank_id=baseline.statement.bank_id,
                             layout_id=baseline.statement.layout_id, account=baseline.statement.account)
        if normalized != baseline.statement or baseline.evidence != new.evidence:
            raise ValueError("The specific baseline differs materially.")
    if new.evidence.financial_coverage_verified != EvidenceStatus.VERIFIED:
        raise ValueError("Financial coverage was not verified.")
    for transaction, source in zip(new.statement.transactions, new.provenance.transactions, strict=True):
        tokens = lambda span: source_tokens(document, span, composed.profile.tolerances)
        if (parse_date(" ".join(tokens(source.date_source))) != transaction.posting_date
                or " ".join(token for span in source.description_sources for token in tokens(span)) != transaction.description
                or parse_money(" ".join(tokens(source.amount_source))).amount.copy_abs() != transaction.amount.copy_abs()
                or source.direction_source is None):
            raise ValueError("A transaction field does not resolve to the source.")
        if transaction.balance_after is not None:
            if parse_money(" ".join(tokens(source.balance_source))).amount != transaction.balance_after:
                raise ValueError("A running balance field does not resolve to the source.")
    # Export comparison uses a separate, fictitious identity solely in memory.
    profile = OFXProfile(bank_id="999", account_id="DEMO-0001", branch_id="0001",
                         organization="Verification fixture", institution_id="999")
    payload = generate_ofx(new.statement, profile)
    if payload != generate_ofx(old.statement, profile) or payload != generate_ofx(new.statement, profile):
        raise ValueError("Legacy/operator exports differ or export is nondeterministic.")
    return {"transactions": expected, "match": True, "legacy_operator_hypothesis_match": True,
            "evidence_match": True, "coverage_verified": True,
            "field_sources_verified": True, "ofx_deterministic": True}


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("first", type=Path)
    arguments.add_argument("second", type=Path)
    args = arguments.parse_args()
    try:
        first = verify(args.first, 29, specific=True)
        second = verify(args.second, 13, specific=False)
    except (ConversionError, ValueError):
        print("M7/M8 equivalence verification failed; no private contents were recorded.")
        return 1
    for label, result in (("inter", first), ("second", second)):
        for key, value in result.items():
            print(f"{label}_{key}: {str(value).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
