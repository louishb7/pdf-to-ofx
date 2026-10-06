# Corpus Lab V1

Banks are sampling sources, not parser boundaries. This laboratory observes the
current engine; it does not introduce institution detection, layouts, financial
grammars, OCR, external services or OFX changes.

## Local area and manifest

```text
.corpus/
  raw/
  manifests/
  reports/
```

The entire tree is Git-ignored. Existing private fixtures remain where they are.
The laboratory never moves originals or downloads documents. Manifests are
authored locally as UTF-8 JSONL, with one document reference per nonblank line:

```json
{"schema_version":1,"corpus_id":"c000001","local_path":"../raw/sample.pdf","sha256":null,"source_url":null,"source_kind":"private_acceptance","institution":null,"country":null,"region":null,"document_kind":"bank_statement","language":null,"public_or_private":"private","retrieved_at":null,"notes":null}
```

Relative paths resolve from the manifest directory; absolute paths may reference
existing documents without copying them. Unknown metadata is `null`. Supported
`source_kind` values are `official_standard`, `official_bank`,
`official_regulator`, `public_example`, `community_example`, `private_acceptance`
and `synthetic`. Visibility is `public`, `private` or `null`. Metadata fields are
text or null; version 1 rejects unknown fields, duplicate JSON keys and unknown
versions. Keep notes about provenance/collection, never statement contents.

The actual file SHA-256 is computed even when the manifest supplies a hash.
Mismatch is an input error; supplied hashes never bypass verification. Identical
bytes share one engine execution within a batch. Every manifest entry still
receives a report row, linked by its one-based `manifest_line`, actual hash,
optional `corpus_id_hash` and `duplicate_of` line. No raw ID is copied to reports.
Cache contents are fingerprints only, not extracted documents or Statements.

## Analysis and fingerprints

```bash
pdf-to-ofx corpus analyze .corpus/manifests/corpus.jsonl
pdf-to-ofx corpus analyze /local/manifest.jsonl -o .corpus/reports/run.jsonl
```

Each unique readable file goes through `analyze_pdf` with default application
settings, exactly as in the GUI. Its verdict is never changed by observation.
`diagnose_analysis` is the projection shared with `diagnose_pdf` and avoids a
second engine execution. Hypothesis counts, elimination constraints and monetary
causes come from the existing `InferenceResult`/`SearchLedger` summaries.
Chronology candidates are the chronologies actually visited in that ledger;
they are `null` before search. Approved role counts come from provenance.

A second local extraction supplies optional structural observations using
`extract_pdf`, `reconstruct_rows`, `date_candidates`, `scan_money_regions`,
existing financial labels and `observe_geometry`. There is no second inference
engine. This additional extraction costs time; V1 favors an independent
observation layer over changing the conversion pipeline. Unknown observations
remain null, with fixed observation-error codes; observer failure preserves the
engine's verdict. Date counts include period/header candidates, not just movement
dates. Monetary counts are lexical regions, not automatically approved movements.
Opening/closing observations mean an existing label or approved balance was
observed; they do not assert validation. Geometry modes are candidates. All
recognition remains limited to the current engine's lexical vocabulary.

Each `StructuralFingerprint` records the requested counts, codes, hypotheses,
constraints, monetary causes, balance observations and approved transaction
count, plus candidate modes, approved chronology/role counts, reconciliation
level, budget exhaustion and observation errors. If the engine supplies no
failure stage, the lab uses `analysis`, or `extraction` for PDF extraction errors.
These are explicitly coarse fallbacks, not inferred substage diagnoses.

`structural_signature` hashes the canonical complete fingerprint.
`outcome_signature` hashes status, failure stage, diagnostic and sets of blocking
capabilities, eliminating constraints and monetary causes. Neither signature
contains document identity or duration. Per-entry `duration_ms` measures hashing,
analysis and observation; duplicate durations measure hash/cache lookup only.
Durations legitimately vary; fingerprints, signatures and aggregates are
deterministic for the same engine and inputs.

Missing/unreadable files, invalid manifest records, hash mismatch, file changes
and unexpected analysis exceptions have fixed `input_error` codes. They do not
become financial `INVALID` verdicts. The batch continues after each such error.
Hashes are checked again after analysis to detect changed originals. Reports
are written atomically; the manifest and referenced originals cannot be replaced.
Output requires `.jsonl`. Analysis and report commands return 1 when their data
contains input/report errors, otherwise 0; rejected financial verdicts are normal
lab results and do not alone change the exit code.

## Aggregation and capability matrix

```bash
pdf-to-ofx corpus report .corpus/reports/latest.jsonl --top 10
pdf-to-ofx corpus report .corpus/reports/latest.jsonl --json
```

Status totals, failure clusters and matrix incidence count unique actual hashes.
Duplicates do not inflate coverage. Input errors are counted per manifest entry
separately; errored duplicates can therefore appear in both entry-level totals.
Failures cluster by blocking capability, falling back to diagnostic/status when
there is no blocking capability. The second grouping uses failure stage plus
the complete sorted capability combination. No grouping uses institution names.

The complete JSON aggregate includes a matrix with `documents`, `success` and
`blocked` for each observed concept. Prefixes distinguish `observed:` lexical
facts, `candidate:` geometry/chronology, `verified:role:` approved provenance,
`failure:` blockers, `constraint:` eliminated hypotheses and
`monetary_diagnostic:` causes. Each document contributes at most once per row.
A constraint may eliminate alternatives even in a successful document; matrix
success incidence does not claim universal support for that structure. Human
output limits each cluster/matrix section to `--top`; JSON retains every row.

## Privacy boundary and fictitious example

Reports never serialize paths, URLs, institution, notes, raw IDs, names, account
identifiers, descriptions, amounts, financial dates, reason strings or exception
messages/tracebacks. The existing engine enums and reporting vocabulary are
allowlisted. Unknown technical labels become stable `unknown_<sha256>` codes,
retaining clustering without exposing their text. Aggregate readers also project
only allowed fields and sanitize labels, including in edited/untrusted reports.
No network requests, downloads, third-party uploads or automatic OFX exports occur.

Generate only the committed, entirely fictitious fixture recipe into a new or
empty directory; the generator refuses a nonempty target:

```bash
python tests/fixtures/corpus_lab/generate.py .corpus/fictitious-example
pdf-to-ofx corpus analyze .corpus/fictitious-example/manifests/corpus.jsonl -o .corpus/fictitious-example/reports/latest.jsonl
pdf-to-ofx corpus report .corpus/fictitious-example/reports/latest.jsonl --top 5
python -m pytest -q tests/corpus
```

The example has six entries: SUCCESS, UNSUPPORTED, a missing file, AMBIGUOUS,
INVALID and a duplicate of SUCCESS. Four unique PDFs are analyzed, with one
verdict of each kind; one entry is deduplicated and one has `file_missing`.
The intentional missing file makes the example commands return 1 while still
producing the complete report. Fixtures contain fictitious privacy sentinels to
verify that neither diagnostic JSONL nor aggregate/CLI output exposes contents.
