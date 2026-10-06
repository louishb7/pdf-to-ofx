"""Real extraction of a fictitious corpus: verdicts, privacy and aggregation."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import analyze_pdf
from pdf_to_ofx.application.diagnose import diagnose_pdf
from pdf_to_ofx.cli import main
from pdf_to_ofx.corpus.batch import analyze_corpus, decode_json, file_sha256
from pdf_to_ofx.corpus.fingerprint import fingerprint_pdf
from pdf_to_ofx.corpus.manifest import CorpusEntry, SourceKind
from pdf_to_ofx.corpus.report import aggregate_report, render_report


FORBIDDEN = ("PESSOA FICTICIA PRIVACIDADE", "000.000.000-00", "DEMO-987654321",
             "DESCRICAO FICTICIA CONFIDENCIAL", "731,42", "731.42", "745,69", "745.69",
             "746,69", "746.69", "14,27", "14.27", "05/08/2028", "2028-08-05",
             "INSTITUICAO FICTICIA PRIVACIDADE", "METADADOS FICTICIOS PARA TESTES", "fictitious-success")


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def write_entries(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def test_actual_batch_verdicts_deduplication_and_continuation(corpus_manifest, tmp_path):
    output = tmp_path / "report.jsonl"
    with patch("pdf_to_ofx.corpus.batch.analyze_pdf", wraps=analyze_pdf) as engine, \
            patch("pdf_to_ofx.application.convert.generate_ofx") as export:
        summary = analyze_corpus(corpus_manifest, output)
    assert (summary.entries, summary.analyzed, summary.duplicates, summary.input_errors) == (6, 4, 1, 1)
    assert engine.call_count == 4
    export.assert_not_called()
    result = records(output)
    assert [r["fingerprint"]["analysis_status"] for r in result] == [
        "success", "unsupported", None, "ambiguous", "invalid", "success",
    ]
    assert result[2]["input_error"] == "file_missing"
    assert result[5]["duplicate_of"] == 1
    assert result[0]["sha256"] == result[5]["sha256"]
    assert result[0]["fingerprint"] == result[5]["fingerprint"]
    assert result[0]["structural_signature"] == result[5]["structural_signature"]
    assert all(r["duration_ms"] >= 0 for r in result)
    assert not list(tmp_path.rglob("*.ofx"))


def test_fingerprint_observes_existing_engine_counts_and_provenance(corpus_manifest):
    path = corpus_manifest.parent.parent / "raw/success.pdf"
    analysis = analyze_pdf(path)
    fingerprint = fingerprint_pdf(path, analysis)
    diagnostic = diagnose_pdf(path)
    assert (fingerprint.page_count, fingerprint.row_count, fingerprint.date_candidate_count,
            fingerprint.money_region_count) == (1, 8, 3, 3)
    assert fingerprint.positioned_text_available
    assert fingerprint.currency_observations == ("BRL",)
    assert fingerprint.chronology_candidates == ("ascending",)
    assert fingerprint.opening_balance_observed and fingerprint.closing_balance_observed
    assert not fingerprint.running_balance_candidate and not fingerprint.group_subtotal_candidate
    assert fingerprint.transaction_count_if_approved == 1
    assert fingerprint.candidate_hypotheses == diagnostic["candidate_hypotheses"]
    assert fingerprint.explored_hypotheses == diagnostic["explored_hypotheses"]
    assert dict(fingerprint.approved_role_counts) == {"closing_balance": 1, "movement": 1, "opening_balance": 1}


def test_reports_never_copy_financial_contents_or_manifest_metadata(corpus_manifest, tmp_path, capsys):
    entry = json.loads(corpus_manifest.read_text().splitlines()[0])
    entry.update(corpus_id="PESSOA FICTICIA PRIVACIDADE", source_url="https://private.invalid/DEMO-987654321",
                 notes="DESCRICAO FICTICIA CONFIDENCIAL 731,42", retrieved_at="2028-08-05")
    write_entries(corpus_manifest, [entry])
    output = tmp_path / "report.jsonl"
    assert main(["corpus", "analyze", str(corpus_manifest), "-o", str(output)]) == 0
    assert main(["corpus", "report", str(output)]) == 0
    assert main(["corpus", "report", str(output), "--json"]) == 0
    captured = capsys.readouterr()
    all_output = output.read_text() + captured.out + captured.err
    for secret in FORBIDDEN + (str(corpus_manifest), "private.invalid"):
        assert secret not in all_output
    assert records(output)[0]["corpus_id_hash"] == hashlib.sha256(entry["corpus_id"].encode()).hexdigest()
    assert "source_url" not in output.read_text()
    assert "local_path" not in output.read_text()


def test_aggregate_groups_by_structure_counts_unique_documents_and_builds_matrix(corpus_manifest, tmp_path):
    output = tmp_path / "report.jsonl"
    analyze_corpus(corpus_manifest, output)
    report = aggregate_report(output)
    assert (report["entries"], report["documents"], report["duplicates"]) == (6, 4, 1)
    assert report["status_counts"] == {"success": 1, "ambiguous": 1, "unsupported": 1, "invalid": 1}
    assert report["input_errors"] == {"file_missing": 1}
    assert report["failure_clusters"] == {"checkpoint_reconciliation": 1, "date_attribution": 1, "material_ambiguity": 1}
    assert report["stage_capability_clusters"]["structural_composition + date_attribution"] == 1
    matrix = report["capability_matrix"]
    assert matrix["observed:money_regions"] == {"documents": 3, "success": 1, "blocked": 2}
    assert matrix["failure:date_attribution"] == {"documents": 1, "success": 0, "blocked": 1}
    assert matrix["verified:role:movement"] == {"documents": 1, "success": 1, "blocked": 0}
    assert "institution" not in json.dumps(report)


def test_signatures_and_aggregate_are_deterministic_while_duration_is_separate(corpus_manifest, tmp_path):
    first, second = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    analyze_corpus(corpus_manifest, first)
    analyze_corpus(corpus_manifest, second)
    one, two = records(first), records(second)
    for record in one + two:
        record.pop("duration_ms")
    assert one == two
    expected = aggregate_report(first)
    assert aggregate_report(second) == expected
    second.write_text("\n".join(reversed(first.read_text().splitlines())) + "\n")
    assert aggregate_report(second) == expected
    assert render_report(aggregate_report(second)) == render_report(expected)


def test_different_institution_metadata_does_not_change_engine_or_grouping(corpus_manifest, tmp_path):
    entries = [json.loads(line) for line in corpus_manifest.read_text().splitlines()]
    first = tmp_path / "first.jsonl"
    analyze_corpus(corpus_manifest, first)
    for entry in entries:
        entry["institution"] = "ANOTHER FICTITIOUS SAMPLING SOURCE"
        entry["country"] = "ZZ"
    write_entries(corpus_manifest, entries)
    second = tmp_path / "second.jsonl"
    analyze_corpus(corpus_manifest, second)
    assert aggregate_report(first) == aggregate_report(second)


def test_unexpected_engine_exception_is_sanitized_and_next_document_runs(corpus_manifest, tmp_path):
    entry = json.loads(corpus_manifest.read_text().splitlines()[1])
    with corpus_manifest.open("a") as manifest:
        manifest.write(json.dumps(entry) + "\n")
    def failing_engine(path):
        if path.stem == "unsupported":
            raise RuntimeError("PESSOA FICTICIA PRIVACIDADE 731,42 DEMO-987654321")
        return analyze_pdf(path)
    output = tmp_path / "report.jsonl"
    with patch("pdf_to_ofx.corpus.batch.analyze_pdf", side_effect=failing_engine):
        summary = analyze_corpus(corpus_manifest, output)
    result = records(output)
    assert summary.input_errors == 3
    assert summary.duplicates == 2
    assert result[1]["input_error"] == "analysis_error"
    assert result[3]["fingerprint"]["analysis_status"] == "ambiguous"
    assert result[4]["fingerprint"]["analysis_status"] == "invalid"
    assert result[-1]["duplicate_of"] == 2
    assert aggregate_report(output)["duplicates"] == 2
    for secret in FORBIDDEN:
        assert secret not in output.read_text()


def test_optional_observer_failure_preserves_the_engine_verdict(corpus_manifest, tmp_path):
    output = tmp_path / "report.jsonl"
    with patch("pdf_to_ofx.corpus.batch.fingerprint_pdf", side_effect=RuntimeError("DESCRICAO FICTICIA CONFIDENCIAL")):
        summary = analyze_corpus(corpus_manifest, output)
    result = records(output)
    assert summary.input_errors == 1
    assert result[0]["fingerprint"]["analysis_status"] == "success"
    assert result[0]["fingerprint"]["observation_errors"] == ["observation_error"]
    assert "DESCRICAO FICTICIA CONFIDENCIAL" not in output.read_text()


def test_unknown_engine_labels_are_hashed_not_printed(corpus_manifest, tmp_path):
    actual = analyze_pdf(corpus_manifest.parent.parent / "raw/unsupported.pdf")
    private = "PESSOA FICTICIA PRIVACIDADE 731,42"
    injected = replace(actual, reason=private, bank_name=private, failure_stage=private,
                       blocking_capabilities=(private,), pruned_constraints=((private, 1),))
    output = tmp_path / "report.jsonl"
    with patch("pdf_to_ofx.corpus.batch.analyze_pdf", return_value=injected):
        analyze_corpus(corpus_manifest, output)
    assert private not in output.read_text()
    assert private not in render_report(aggregate_report(output))
    assert "unknown_" in output.read_text()


def test_invalid_manifest_lines_and_hash_mismatch_do_not_stop_the_batch(corpus_manifest, tmp_path):
    good = json.loads(corpus_manifest.read_text().splitlines()[0])
    wrong_hash = {**good, "sha256": "0" * 64}
    missing_path = {"schema_version": 1, "local_path": None}
    invalid_path = {"schema_version": 1, "local_path": "\u0000PRIVATE_PATH"}
    corpus_manifest.write_bytes(b'not JSON\n\xff\n' + "".join(json.dumps(e) + "\n" for e in [
        {"schema_version": 2}, wrong_hash, missing_path, invalid_path, good,
    ]).encode())
    output = tmp_path / "report.jsonl"
    summary = analyze_corpus(corpus_manifest, output)
    assert summary.entries == 7 and summary.input_errors == 6
    assert [r["input_error"] for r in records(output)] == [
        "manifest_invalid", "manifest_invalid", "manifest_invalid", "hash_mismatch", "path_missing", "path_invalid", None,
    ]
    assert records(output)[-1]["fingerprint"]["analysis_status"] == "success"
    assert "PRIVATE_PATH" not in output.read_text()


def test_supplied_hash_is_verified_instead_of_used_as_dedup_key(corpus_manifest, tmp_path):
    entry = json.loads(corpus_manifest.read_text().splitlines()[0])
    path = corpus_manifest.parent / entry["local_path"]
    entry["sha256"] = file_sha256(path).upper()
    write_entries(corpus_manifest, [entry, entry])
    output = tmp_path / "report.jsonl"
    summary = analyze_corpus(corpus_manifest, output)
    assert (summary.analyzed, summary.duplicates, summary.input_errors) == (1, 1, 0)
    assert records(output)[0]["sha256"] == file_sha256(path)


def test_aggregate_ignores_extra_metadata_and_sanitizes_untrusted_labels(corpus_manifest, tmp_path):
    output = tmp_path / "report.jsonl"
    analyze_corpus(corpus_manifest, output)
    data = records(output)
    secret = "PESSOA FICTICIA PRIVACIDADE 731,42"
    for record in data:
        record["name"] = record["institution"] = secret
        record["fingerprint"]["raw_text"] = secret
    data[1]["fingerprint"]["blocking_capabilities"] = [secret]
    data[1]["fingerprint"]["failure_stage"] = secret
    write_entries(output, data)
    output.write_text(output.read_text() + 'not JSON\n{"schema_version":2}\n')
    report = aggregate_report(output)
    assert report["input_errors"] == {"file_missing": 1, "report_invalid": 2}
    assert secret not in json.dumps(report)
    assert secret not in render_report(report)


@pytest.mark.parametrize("kind", list(SourceKind))
def test_manifest_accepts_all_requested_source_kinds(kind):
    assert CorpusEntry.from_dict({"source_kind": kind.value}).source_kind == kind


@pytest.mark.parametrize("entry", [{"schema_version": True}, {"schema_version": 2}, {"source_kind": "unknown"},
                                   {"sha256": "bad"}, {"notes": 123}, {"unknown_field": None},
                                   {"public_or_private": "unclassified"}, {"corpus_id": "\ud800"}])
def test_manifest_rejects_invalid_or_unknown_schema(entry):
    with pytest.raises(ValueError):
        CorpusEntry.from_dict(entry)


def test_manifest_accepts_null_metadata_and_rejects_repeated_json_fields():
    assert CorpusEntry.from_dict({"local_path": None, "institution": None}).institution is None
    with pytest.raises(ValueError):
        decode_json('{"local_path":"first","local_path":"second"}')


def test_cli_default_output_exit_codes_and_safe_configuration_errors(corpus_manifest, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["corpus", "analyze", str(corpus_manifest)]) == 1  # one intentionally missing file
    output = tmp_path / ".corpus/reports/latest.jsonl"
    assert output.is_file()
    assert main(["corpus", "report", str(output), "--top", "3"]) == 1
    assert "Corpus: 4 documents" in capsys.readouterr().out
    private_path = tmp_path / "PESSOA FICTICIA PRIVACIDADE.jsonl"
    assert main(["corpus", "analyze", str(private_path)]) == 1
    assert "PESSOA FICTICIA PRIVACIDADE" not in capsys.readouterr().err


def test_report_cannot_replace_manifest_or_document(corpus_manifest, tmp_path):
    original = corpus_manifest.read_bytes()
    with pytest.raises(ValueError):
        analyze_corpus(corpus_manifest, corpus_manifest)
    assert corpus_manifest.read_bytes() == original
    protected = tmp_path / "document.jsonl"
    protected.write_bytes((corpus_manifest.parent.parent / "raw/success.pdf").read_bytes())
    document_bytes = protected.read_bytes()
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(protected)
    write_entries(corpus_manifest, [{"local_path": str(protected)}])
    with pytest.raises(ValueError):
        analyze_corpus(corpus_manifest, alias)
    assert protected.read_bytes() == document_bytes
    assert not list(tmp_path.glob(".corpus-report-*"))


def test_changed_document_is_not_attached_to_the_preanalysis_hash(corpus_manifest, tmp_path):
    entry = json.loads(corpus_manifest.read_text().splitlines()[0])
    write_entries(corpus_manifest, [entry])
    output = tmp_path / "report.jsonl"
    with patch("pdf_to_ofx.corpus.batch.file_sha256", side_effect=["1" * 64, "2" * 64]):
        summary = analyze_corpus(corpus_manifest, output)
    assert summary.input_errors == 1
    record = records(output)[0]
    assert record["input_error"] == "file_changed"
    assert record["fingerprint"]["analysis_status"] is None


def test_optional_structural_properties_are_unknown_when_pdf_extraction_fails(corpus_manifest, tmp_path):
    malformed = tmp_path / "fictitious-broken.pdf"
    malformed.write_bytes(b"not a PDF")
    write_entries(corpus_manifest, [{"local_path": str(malformed)}])
    output = tmp_path / "report.jsonl"
    analyze_corpus(corpus_manifest, output)
    fingerprint = records(output)[0]["fingerprint"]
    assert fingerprint["analysis_status"] == "unsupported"
    assert fingerprint["failure_stage"] == "extraction"
    assert fingerprint["row_count"] is None
    assert fingerprint["opening_balance_observed"] is None
    assert fingerprint["observation_errors"] == ["extraction_unavailable"]
