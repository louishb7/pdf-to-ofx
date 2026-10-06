"""Deterministic document incidence, failure clusters and capability matrix."""

from collections import Counter
import re
from pathlib import Path

from pdf_to_ofx.corpus.batch import decode_json
from pdf_to_ofx.corpus.fingerprint import StructuralFingerprint
from pdf_to_ofx.corpus.privacy import technical_code
from pdf_to_ofx.domain.evidence import AnalysisStatus
from pdf_to_ofx.domain.models import Chronology
from pdf_to_ofx.generic.profile import AmountMode, BalanceMode, DateMode


INPUT_ERRORS = frozenset({"manifest_invalid", "path_missing", "path_invalid", "file_missing",
                          "file_unreadable", "hash_mismatch", "analysis_error", "file_changed"})


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid diagnostic count.")
    return value


def _codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError("Invalid diagnostic codes.")
    return tuple(sorted({technical_code(v) for v in value}))


def _counter(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise ValueError("Invalid diagnostic counter.")
    return tuple(sorted((technical_code(k), _count(v)) for k, v in value.items()))


def _fingerprint(value: object) -> StructuralFingerprint:
    """Read only the V1 allowlist; extra metadata can never reach rendering."""
    if not isinstance(value, dict):
        raise ValueError("Invalid fingerprint.")
    data = {}
    for key in ("page_count", "row_count", "date_candidate_count", "money_region_count", "transaction_count_if_approved"):
        data[key] = _count(value[key]) if value.get(key) is not None else None
    for key in ("candidate_hypotheses", "explored_hypotheses"):
        data[key] = _count(value.get(key, 0))
    for key in ("positioned_text_available", "opening_balance_observed", "closing_balance_observed",
                "running_balance_candidate", "group_subtotal_candidate", "budget_exhausted"):
        item = value.get(key, False if key == "budget_exhausted" else None)
        if item is not None and type(item) is not bool:
            raise ValueError("Invalid diagnostic flag.")
        data[key] = item
    for key, enum in (("analysis_status", AnalysisStatus), ("approved_chronology", Chronology),
                      ("date_mode_candidate", DateMode), ("amount_mode_candidate", AmountMode),
                      ("balance_mode_candidate", BalanceMode)):
        data[key] = enum(value[key]).value if value.get(key) is not None else None
    for key in ("failure_stage", "diagnostic", "reconciliation_level"):
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ValueError("Invalid diagnostic label.")
        data[key] = technical_code(item) if item is not None else None
    for key in ("blocking_capabilities", "observation_errors"):
        data[key] = _codes(value.get(key, []))
    for key in ("pruned_constraints", "monetary_diagnostic_causes", "approved_role_counts"):
        data[key] = _counter(value.get(key, {}))
    currencies = value.get("currency_observations", [])
    if not isinstance(currencies, list) or any(not isinstance(c, str) or not re.fullmatch(r"[A-Z]{3}", c) for c in currencies):
        raise ValueError("Invalid observed currency codes.")
    data["currency_observations"] = tuple(sorted(set(currencies)))
    chronologies = value.get("chronology_candidates")
    if chronologies is not None:
        if not isinstance(chronologies, list):
            raise ValueError("Invalid chronology candidates.")
        chronologies = tuple(sorted({Chronology(c).value for c in chronologies}))
    data["chronology_candidates"] = chronologies
    return StructuralFingerprint(**data)


def capability_incidence(fingerprint: StructuralFingerprint) -> set[str]:
    """Presence per document, not occurrence totals or universal support claims."""
    capabilities = set()
    for name, present in (
        ("positioned_text", fingerprint.positioned_text_available),
        ("dates", bool(fingerprint.date_candidate_count)),
        ("money_regions", bool(fingerprint.money_region_count)),
        ("opening_balance", fingerprint.opening_balance_observed),
        ("closing_balance", fingerprint.closing_balance_observed),
    ):
        if present:
            capabilities.add("observed:" + name)
    for name, present in (("running_balance", fingerprint.running_balance_candidate),
                          ("group_subtotal", fingerprint.group_subtotal_candidate)):
        if present:
            capabilities.add("candidate:" + name)
    capabilities.update("observed:currency:" + c for c in fingerprint.currency_observations)
    capabilities.update("candidate:chronology:" + c for c in fingerprint.chronology_candidates or ())
    for name, value in (("date_mode", fingerprint.date_mode_candidate), ("amount_mode", fingerprint.amount_mode_candidate),
                        ("balance_mode", fingerprint.balance_mode_candidate)):
        if value:
            capabilities.add("candidate:" + name + ":" + value)
    capabilities.update("verified:role:" + role for role, count in fingerprint.approved_role_counts if count)
    capabilities.update("failure:" + c for c in fingerprint.blocking_capabilities)
    capabilities.update("constraint:" + c for c, count in fingerprint.pruned_constraints if count)
    capabilities.update("monetary_diagnostic:" + c for c, count in fingerprint.monetary_diagnostic_causes if count)
    return capabilities


def aggregate_report(path: Path) -> dict[str, object]:
    entries = duplicates = documents = 0
    seen = set()
    statuses, errors, failures, combinations, signatures = Counter(), Counter(), Counter(), Counter(), Counter()
    matrix: dict[str, dict[str, int]] = {}
    with path.open("rb") as source:
        for raw in source:
            if not raw.strip():
                continue
            entries += 1
            try:
                record = decode_json(raw.decode("utf-8"))
                if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
                    raise ValueError("Unsupported report schema.")
                error = record.get("input_error")
                if error is not None:
                    if error not in INPUT_ERRORS:
                        raise ValueError("Invalid input error code.")
                    if record.get("duplicate_of") is not None:
                        if _count(record["duplicate_of"]) == 0:
                            raise ValueError("Invalid duplicate reference.")
                        duplicates += 1
                    errors[error] += 1
                    continue
                digest = record.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Invalid document hash.")
                fingerprint = _fingerprint(record.get("fingerprint"))
                if fingerprint.analysis_status is None:
                    raise ValueError("Missing engine verdict.")
            except (ValueError, TypeError, UnicodeError):
                errors["report_invalid"] += 1
                continue
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            documents += 1
            statuses[fingerprint.analysis_status] += 1
            signatures[fingerprint.outcome_signature] += 1
            success = fingerprint.analysis_status == AnalysisStatus.SUCCESS
            if not success:
                codes = fingerprint.blocking_capabilities or (fingerprint.diagnostic or fingerprint.analysis_status,)
                failures.update(codes)
                combination = (fingerprint.failure_stage or "analysis") + " + " + ",".join(codes)
                combinations[combination] += 1
            for capability in capability_incidence(fingerprint):
                incidence = matrix.setdefault(capability, {"documents": 0, "success": 0, "blocked": 0})
                incidence["documents"] += 1
                incidence["success" if success else "blocked"] += 1
    ranked = lambda counter: dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))
    return {
        "schema_version": 1, "entries": entries, "documents": documents, "duplicates": duplicates,
        "status_counts": {status.value: statuses[status.value] for status in AnalysisStatus},
        "input_errors": dict(sorted(errors.items())),
        "failure_clusters": ranked(failures), "stage_capability_clusters": ranked(combinations),
        "outcome_signatures": dict(sorted(signatures.items())),
        "capability_matrix": dict(sorted(matrix.items())),
    }


def render_report(report: dict[str, object], top: int = 10) -> str:
    lines = [f"Corpus: {report['documents']} documents ({report['entries']} entries; {report['duplicates']} duplicates)"]
    lines.extend(f"{status.upper():<23} {count:>5}" for status, count in report["status_counts"].items())
    if report["input_errors"]:
        lines.append("\nInput/report errors")
        lines.extend(f"{count:>5}  {code}" for code, count in report["input_errors"].items())
    for label, key in (("Top failure clusters", "failure_clusters"),
                       ("Failure stage + blocking capabilities", "stage_capability_clusters")):
        lines.append("\n" + label)
        lines.extend(f"{count:>5}  {code}" for code, count in list(report[key].items())[:top])
        if not report[key]:
            lines.append("    0  none")
    lines.append("\nCapability matrix: documents / SUCCESS / blocked")
    matrix = sorted(report["capability_matrix"].items(), key=lambda item: (-item[1]["documents"], item[0]))
    lines.extend(f"{counts['documents']:>5} / {counts['success']:>5} / {counts['blocked']:>5}  {code}"
                 for code, counts in matrix[:top])
    return "\n".join(lines)
