"""Streaming local batch analysis, deduplicated by actual file contents."""

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

from pdf_to_ofx.application.convert import analyze_pdf
from pdf_to_ofx.corpus.fingerprint import StructuralFingerprint, fingerprint_analysis, fingerprint_pdf
from pdf_to_ofx.corpus.manifest import CorpusEntry
from pdf_to_ofx.corpus.privacy import opaque_hash


@dataclass(frozen=True, slots=True)
class BatchSummary:
    entries: int
    analyzed: int
    duplicates: int
    input_errors: int


def decode_json(text: str) -> object:
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Repeated JSON field.")
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=unique_keys)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def analyze_corpus(manifest: Path, output: Path) -> BatchSummary:
    """One report row per nonblank manifest row; errors never include free text.

    Relative PDF paths are resolved from the manifest's directory. All engine
    verdicts come from analyze_pdf. No account data, context or profiles supplied.
    """
    if manifest.resolve() == output.resolve():
        raise ValueError("Report must not replace its manifest.")
    if output.suffix.lower() != ".jsonl":
        raise ValueError("Corpus reports require a JSONL output path.")
    cache: dict[str, tuple[int, StructuralFingerprint, str | None]] = {}
    entries = analyzed = duplicates = input_errors = 0
    temporary = None
    with manifest.open("rb") as source:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                    prefix=".corpus-report-", suffix=".jsonl", delete=False) as report:
                temporary = Path(report.name)
                for line_number, raw in enumerate(source, 1):
                    if not raw.strip():
                        continue
                    entries += 1
                    started = perf_counter()
                    fingerprint = StructuralFingerprint()
                    entry = None
                    digest = duplicate_of = error = None
                    try:
                        entry = CorpusEntry.from_dict(decode_json(raw.decode("utf-8")))
                    except (ValueError, UnicodeError):
                        error = "manifest_invalid"
                    if entry is not None:
                        if not entry.local_path or not entry.local_path.strip():
                            error = "path_missing"
                        else:
                            path = Path(entry.local_path)
                            if not path.is_absolute():
                                path = manifest.parent / path
                            path_is_output = False
                            try:
                                path_is_output = path.resolve() == output.resolve()
                                if path_is_output:
                                    raise ValueError("Report must not replace a corpus document.")
                                digest = file_sha256(path)
                            except FileNotFoundError:
                                error = "file_missing"
                            except OSError:
                                error = "file_unreadable"
                            except (ValueError, RuntimeError):
                                error = "path_invalid"
                                if path_is_output:
                                    raise
                            if digest is not None:
                                if entry.sha256 is not None and entry.sha256 != digest:
                                    error = "hash_mismatch"
                                elif digest in cache:
                                    duplicate_of, fingerprint, error = cache[digest]
                                    duplicates += 1
                                else:
                                    analyzed += 1
                                    try:
                                        analysis = analyze_pdf(path)
                                    except Exception:
                                        # Do not serialize exception text, repr or traceback.
                                        error = "analysis_error"
                                    else:
                                        try:
                                            fingerprint = fingerprint_pdf(path, analysis)
                                        except Exception:
                                            fingerprint = replace(fingerprint_analysis(analysis),
                                                                  observation_errors=("observation_error",))
                                    try:
                                        if file_sha256(path) != digest:
                                            error = "file_changed"
                                    except OSError:
                                        error = "file_changed"
                                    if error == "file_changed":
                                        fingerprint = StructuralFingerprint()
                                    else:
                                        cache[digest] = (line_number, fingerprint, error)
                    input_errors += error is not None
                    record = {
                        "schema_version": 1, "manifest_line": line_number,
                        "corpus_id_hash": opaque_hash(entry.corpus_id) if entry and entry.corpus_id else None,
                        "sha256": digest, "duplicate_of": duplicate_of,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "input_error": error, "fingerprint": fingerprint.to_dict(),
                        "structural_signature": fingerprint.structural_signature,
                        "outcome_signature": fingerprint.outcome_signature,
                    }
                    report.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")
            temporary.replace(output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return BatchSummary(entries, analyzed, duplicates, input_errors)
