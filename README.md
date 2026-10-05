# PDF to OFX

An offline conversion core for an accounting office that receives PDF bank
statements and uses OFX in its Athenas workflow. Financial document contents are
processed locally, without external services, APIs, telemetry, or persistence
beyond the requested output file.

## Current status: M0 through M12 — Structural monetary roles and bounded hypotheses

M0 proves a synthetic PDF → normalized statement → validation → OFX pipeline.
M1 adds initial support for the **investigated Banco Inter digital layout**,
validated locally against one private PDF and its corresponding official OFX.
This is not general support for every Banco Inter statement. Only digital PDFs
with extractable text are supported; pages without usable text fail explicitly.
Malformed rows, unsupported layout variations and balance mismatches stop
conversion. There is no OCR; processing remains entirely local.
M2 hardens deterministic OFX export and tests its financial semantics against
a small, fully fictitious public reference.
M3 adds a small PySide6 desktop interface for selecting a PDF, reviewing a
validated statement and explicitly saving its OFX. The CLI remains available.
M4 adds positioned extraction, visual row reconstruction, portable structural
profiles and conservative generic inference. The proven Inter and Synthetic
parsers remain the default for their recognized layouts. Unknown institutions
can now be interpreted structurally, with OFX export gated on explicit metadata.
M5 validates a second real layout without adding a bank parser. It adds unsigned
movements inside explicitly signed flow subtotals, wrapped descriptions across
pages and conservative handling of recurring document frames. Interpretation
and financial proof use the PDF alone; an official OFX is an optional external
acceptance reference, never an input required by the engine.
M6 separates approved interpretation from OFX export readiness. It adds immutable
financial evidence and index-only provenance, checks monetary coverage in the
existing grammars, and keeps interpreted statements visible in the GUI when
account metadata is missing. It adds no new layout family.
M7 composes generic interpretation from small structural operators, preserves
the legacy grammars for comparison, records sources for every transaction field
and separates document order from economic order. Multiline descriptions and
page frames can now be combined with signed values or C/D markers. It adds no
new bank parser, runtime dependency or support for the third private layout.

**Compatibilidade com Athenas ainda não validada.** The provisional OFX 1.02
profile uses fictitious account metadata (`000` / `SYNTHETIC-DEMO`) only for the
Synthetic Bank fixture. Inter exports use account/branch identifiers extracted
from the document and explicit institution metadata for BRL checking accounts.
Missing or conflicting metadata prevents export. Controlled Athenas import
testing is still required before real use.

## Setup and verification

Python 3.12 or newer is required. On Debian/Ubuntu, install the matching
`python3-venv` system package if Python reports that `ensurepip` is missing.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
pdf-to-ofx-gui
pdf-to-ofx tests/fixtures/synthetic/statement.pdf -o /tmp/synthetic-statement.ofx
pdf-to-ofx tests/fixtures/inter/statement.pdf -o /tmp/fictitious-inter.ofx
```

Dependency installation needs package access; conversion itself works offline.
After updating the checkout, rerun `python -m pip install -e '.[test]'` to install
PySide6 and register the desktop entry point. The GUI needs a graphical desktop;
the CLI remains usable without one.
On Debian/Ubuntu with X11, Qt also needs the system library `libxcb-cursor0`.
If Qt reports that its `xcb` platform plugin cannot load because this library
is missing, install it with `sudo apt install libxcb-cursor0`.

## Desktop use during development

Start the application with either:

```bash
pdf-to-ofx-gui
# Equivalent module entry point:
python -m pdf_to_ofx.ui
```

1. Click **Selecionar PDF**, or drag one local PDF onto the window.
2. Review the bank/layout, period, counts, balances and validation result.
3. Review the read-only transaction table. Amounts use Brazilian formatting
   only in the interface; financial domain values remain exact `Decimal` values.
4. If export requirements are satisfied, click **Salvar OFX** and choose a new
   destination using the system dialog.
   The suggested filename follows the PDF name; existing files are refused.

OFX contents remain in memory until saving is requested. Opening a new PDF clears
the previous result immediately, including after a failed conversion; an old OFX
cannot remain exportable. Cancelling a dialog makes no changes. Missing balances
are displayed as **Não informado pelo extrato**. Conversion failures appear in
Portuguese without document contents or tracebacks.
An approved interpretation without account metadata keeps its summary and table
visible, with **Salvar OFX** disabled and a message explaining the missing data.
The validation summary distinguishes opening/closing reconciliation from a
verified running-balance chain whose opening balance was not supplied.

The GUI and CLI call the same application services. Processing is local and
does not send statements over the internet. There is no conversion history,
automatic financial persistence, telemetry, editing, or persistent settings.
The investigated Inter digital layout and Synthetic Bank demonstration remain
the established end-to-end paths. Generic structures can also be recognized,
but missing institution/account metadata prevents saving. There is no interface
for supplying metadata or teaching layouts yet. This is not general support for
every bank or every Inter statement. **Compatibilidade com Athenas ainda não validada.**

Conversion is synchronous in M3: the public one/two-page fixtures took less than
30 ms in the initial local measurement. Large PDFs may pause the interface;
responsiveness must be reassessed with representative larger inputs before
distribution. No Windows executable or installer is provided yet.

GUI behavior tests use public fixtures and set `QT_QPA_PLATFORM=offscreen` only
inside the GUI test environment. No extra Qt test framework or CI is required:

```bash
python -m pytest tests/ui -q
```

## CLI and public fixtures

Without `-o`, the CLI writes next to the input with an `.ofx` suffix. It refuses
to overwrite existing files, returns nonzero on failure, and prints counts and
balances for Synthetic Bank without transaction descriptions. Real-bank
summaries omit financial amounts and account identifiers.

The committed fixture contains only fictitious data, with opening balance
1000.00, four signed transactions (500.00, -35.00, -200.00, 100.00), and closing
balance 1365.00. Regenerate it reproducibly with:

```bash
python tests/fixtures/synthetic/generate.py
python tests/fixtures/inter/generate.py
```

ReportLab is used only for fixture generation/tests and pinned to keep the PDF
bytes reproducible, including metadata; pytest is the test runner.
Runtime dependencies are pdfplumber, isolated in the extraction module, and
PySide6 for native Qt widgets/dialogs in the desktop layer. A normal virtual
environment and pip are sufficient; Qt Designer, QML and external themes are
not used. Importing the CLI or conversion core does not import the GUI or Qt.

The public Inter fixture is entirely fictitious: it exercises multiple dates,
same-day credits/debits, running balances and a date heading at the end of page
one whose first transaction appears on page two. Private files are never needed
by the automated suite.

## Architecture

`application.convert.analyze_pdf(Path(...))` is the GUI-independent analysis API.
`convert_pdf` remains the CLI convenience wrapper for analysis plus export:

```text
Local PDF → extracted pages → visual structure → semantic candidates
          → structural operators → hypotheses → constraints + source ownership
          → interpretation + financial evidence
          → approved Statement → independent OFX readiness → provisional OFX
```

Bank parsers receive our own document model. Extraction preserves both page text
and immutable `Word` values with page, `x0`, `x1`, `top` and `bottom`.
Pages also retain their original width/height. `pdf.geometry` exposes normalized
coordinates, row proximity, alignment and distance helpers; original coordinates
and the established row reconstruction remain intact. Third-party
PDF objects never leave the extraction module. Coordinates use geometric numeric
values; monetary values continue to use `Decimal` exclusively.
The OFX generator receives domain
values and also validates direct callers. Monetary values use `Decimal`, positive
for credits and negative for debits, with exact cent reconciliation. Normalized
transactions retain document order. Parsers explicitly declare when that order
is ascending economic order; the central validator does not infer it from the
bank identity. Sparse running checkpoints can be validated without inventing
missing balances, while the existing running-balance profile still requires all
its source balances. Export sorts a copy.
Unsupported text is rejected rather than ignored.

Inter preserves the balance after each transaction and checks every adjacent
balance, each daily closing balance and the statement closing balance. Its
opening balance remains `None` when undeclared. This verifies the visible chain,
not a complete reconciliation against an independently supplied opening balance;
the first transaction cannot be checked arithmetically against an absent value.
Detection requires the institution/account header and the known layout markers;
an institution name mentioned in a transaction is insufficient.

The shared `application.export.write_ofx` service publishes complete exports
for both CLI and GUI without replacing existing files using a
temporary file and a hard link in the destination directory. Filesystems without
hard-link support fail explicitly; output permissions are owner-only on POSIX.

The `ui/` package owns only the window, presentation and desktop entry points.
It calls `analyze_pdf`, `assess_export_readiness`, `export_analysis` and
`application.export.write_ofx`;
bank parsing, PDF extraction, financial validation and OFX generation remain in
the existing core. The transaction table follows document order for reviewing
running balances, while the export retains M2's canonical ordering.

## Generic structural interpretation

The `generic/` package has five small responsibilities: reconstruct rows,
recognize exact dates/money/balance labels, represent a layout, interpret its
transactions, and test candidate layouts against financial invariants. It has
no bank-name detector or institution registry, and does not call `InterParser`.

```text
PDF → own positioned words → visual rows → monetary regions
    → structural hypotheses → exact financial validation → Statement
```

`LayoutProfile` describes grouped/per-transaction dates, signed amounts, C/D
markers or signed flow groups, running/absent balances, monetary-region indices,
page date continuity, description boundaries, frame row counts and geometric
tolerances. It contains no financial document data.
`to_json()` / `from_json()` are deterministic, versioned and reject unknown
fields or duplicate keys. No profiles are written automatically.

Tolerances are centralized in `generic.structure.Tolerances`: row centers may
vary by 3 PDF points, tokens within a monetary cell by 12 points, and a wrapped
summary value may follow its aligned label by at most 30 points. These are
explicitly configurable/tested. Fixed row anchors prevent adjacent lines from
being merged by a chain of nearby words. Monetary regions are spatially adjacent
token runs in left-to-right order; this version does not require globally aligned
columns, since some PDFs position amounts immediately after variable descriptions.

Inference enumerates the supported date/amount/balance modes and possible
movement/balance assignments. A hypothesis must consume every body row and pass
`validate_statement`, including each exact running-balance link and declared
daily/statement balances. A materially unique supported interpretation with
coverage and sufficient financial evidence yields `success`; remaining
ambiguity or insufficient evidence yields `ambiguous`; unidentified structures
yield `unsupported`; identified financial contradictions yield `invalid`.
Equivalent profile syntax with identical financial output, evidence and source
ownership does not introduce material ambiguity. Without an opening balance, at
least two running-balance transactions are required for automatic inference.
Absent-balance inference requires independent opening/closing reconciliation.
Explicit profiles can produce a valid domain model with less evidence, but
application analysis will not approve it for export without the same minimum
financial-evidence and coverage policy.

Generic period and balance metadata are read from exact Portuguese financial
labels, separately from the profile. A statement period must be declared or
explicitly supplied; coverage is never guessed from the transaction dates.
Missing opening/closing balances remain `None`. Account/institution identity is
supplied independently through `StatementContext`; it is never inferred from
column structure. Unrecognized financial header content is rejected. Automatic
footer recognition is limited to contact roles; explicit footer counts still
cannot suppress monetary or dated content. Unexpected body text is rejected,
including incomplete transactions; arbitrary repeated descriptions are not
treated as footers.

Example of generic interpretation of the public fixture:

```python
from pathlib import Path
from pdf_to_ofx.pdf.extractor import extract_pdf
from pdf_to_ofx.generic.inference import InferenceStatus, infer_layout
from pdf_to_ofx.generic.parser import GenericStatementParser

document = extract_pdf(Path("tests/fixtures/inter/statement.pdf"))
inference = infer_layout(document)
if inference.status == InferenceStatus.SUCCESS:
    assert inference.profile is not None
    statement = GenericStatementParser().interpret_composed(document, inference.profile).statement
    portable_profile = inference.profile.to_json()  # structural data only
```

`application.convert.parse_pdf` returns a validated `ParsedStatement` even when
OFX metadata is missing. `convert_pdf` also generates OFX and therefore fails
without that metadata. Both accept an explicit `layout_profile` and separate
`context`. For known layouts, default calls preserve the specific parsers; a
failure in a recognized parser is never silently bypassed. For an unknown bank,
default parsing attempts generic inference. Supplying sufficient explicit
identity allows generic conversion without registering another bank parser.
The fictitious OFX fallback is restricted to the original Synthetic Bank layout,
including when a generic context contains the reserved test marker.

### Analysis, evidence and export readiness

`StatementAnalysis` distinguishes `SUCCESS`, `AMBIGUOUS`, `UNSUPPORTED` and
`INVALID`. Failure states have no approved Statement. Identity in legacy
`StatementContext` never selects the parser or changes analysis/evidence;
`parse_pdf` attaches that identity only after analysis for compatibility.
The historical Inter grammar remains the default baseline for its recognized
header, while an explicit structural profile uses the generic engine.

```python
from pathlib import Path
from pdf_to_ofx.application.convert import (
    ExportStatus, analyze_pdf, assess_export_readiness, export_analysis,
)
from pdf_to_ofx.domain.evidence import AnalysisStatus

analysis = analyze_pdf(Path("extrato.pdf"))
if analysis.status == AnalysisStatus.SUCCESS:
    statement = analysis.statement  # reviewable, even without account identity
    readiness = assess_export_readiness(analysis)
    if readiness.status == ExportStatus.READY_TO_EXPORT:
        ofx = export_analysis(analysis)  # in memory; no automatic output file
```

The readiness/export APIs also accept explicit `BankAccount` or `OFXProfile`
metadata, independently of interpretation. Missing identity yields
`EXPORT_METADATA_REQUIRED`; missing closing balance or invalid/conflicting
export requirements yields `EXPORT_REQUIREMENTS_MISSING`. Unapproved analysis
yields `EXPORT_BLOCKED`. A complete profile yields `READY_TO_EXPORT`; the
generator still detects failures such as FITID collisions at generation time.
Metadata cannot override conflicting identity already extracted from the PDF.
No metadata-entry form has been added.

`EvidenceReport` records domain validity, declared economic order, verified
running links (including whether the first movement had an opening reference),
opening/closing reconciliation, group subtotals, credit/debit totals, daily
balances and financial coverage. Each verification is `verified`,
`not_available` or `not_applicable`; absence is not a failed check. A
contradiction instead prevents approval. The current minimum acceptance policy
requires independent opening/closing reconciliation or at least one verified
running-balance link, together with complete supported monetary coverage.

`Provenance` holds page/row/word indexes, not duplicate raw text. Generic sources
reference visual rows reconstructed before frame removal; historical parsers
explicitly reference nonblank text lines. Date/flow context and continuation
rows remain associated with each transaction occurrence, including duplicates.
The ephemeral coverage ledger inventories monetary regions before candidate
selection, and each must receive a recognized role: movement, balance,
subtotal, total or an explicitly recognized summary component/adjustment.
There is no arbitrary ignore role. The coverage guarantee is limited to the
existing supported grammars and their financial-token recognition, not all PDFs.
The fictitious `financial_coverage` recipe proves that omitting cancelling
`+20` and `-20` cannot pass merely because `100 + 5 = 105` still reconciles.

Public structural fixtures in `tests/fixtures/layouts/` contain only fictitious
positioned cells. Tests generate reproducible PDFs for grouped dates plus signed
amounts/running balances and per-row dates, including cross-page date context.
The public Inter PDF is compared directly against the specific parser, with only
account identity supplied separately; periods, transactions and balances come
entirely from generic interpretation.

The M5 `group_subtotal` family requires grouped dates, unsigned transaction
amounts, explicitly signed credit/debit subtotals and declared opening/closing
balances. Dates can use abbreviated Portuguese months. Every group must contain
transactions and reconcile exactly; optional statement credit/debit totals are
also checked. No operation keyword is used to infer direction. Nonzero summary
adjustments without an interpretation as detailed transactions are rejected.

`generic.grouped` retains the legacy subtotal grammar for regression comparison.
Its semantic controls, exact aggregation and page frames now reuse operators.
The default generic inference and explicit application profiles use
`generic.composition.interpret_composed`, which never invokes a full legacy
grammar. Continuation text
must lie in an observed description region before the numeric region, including
across page boundaries. Profile geometry and row counts are portable, structural
data only. Inference recognizes exact recurring headers/contact footers; parsing
checks their declarations and any printed page count. Trailing notes may lie
outside the transaction column, but cannot contain dated or monetary content.
Subtotals and full-statement reconciliation still apply after frame removal.

The public `grouped_subtotals_wrapped` fixture is deliberately fictitious and
uses different column positions and frame counts from the investigated private
layout. Tests include alternative positions, arbitrary operation labels,
duplicates, missing pages, malformed frames and independent Decimal contexts.
Legacy M4 JSON profiles remain readable; no automatic profile storage was added.

Current limits: no debit/credit split columns, automatic discovery of arbitrary
wrapped descriptions in every layout, multiple transactions on one visual row,
repeated body headers, arbitrary footer
formats, other monetary locales, OCR, or unvalidated reverse chronological
layouts. There is no wizard, profile storage, general support for whole banks, or Windows
packaging. An additional unpaired PDF from a third institution was investigated
only; reverse ordering with separate daily balances remains unsupported. Lack
of official OFX is not a limitation by itself: PDF financial declarations and
invariants provide the evidence required for interpretation. A future wizard
can create profiles for the supported families and pass identity separately
without changing the parser API; new structures should combine explicit
capabilities and regression fixtures rather than introduce full grammars.

OFX format choices and metadata live in `ofx/generator.py`. The output remains
OFX 1.02 SGML with closed tags, declared `USASCII` / `CHARSET:NONE` and actual
ASCII bytes. Accents are preserved through numeric character references. The
CLI uses this same encoding. Dates use `YYYYMMDD`, without invented times or
timezones; `DTSERVER` and `DTASOF` use the statement end date.

Export order is ascending calendar date, then description (Unicode lexical
order), then signed amount. Declared source chronology and available running
controls must pass validation before sorting; contradictions in that declared
order are rejected. FITIDs use
SHA-256 of normalized transaction/account identity plus an occurrence number
for identical duplicates. Duplicate transactions remain present with unique,
stable IDs; an identifier collision fails explicitly. Sorting does not change
transaction identity. Monetary values have two decimal places; signed zero is
canonicalized to `0.00`, including in FITID identity. Identical normalized inputs
produce identical bytes without randomness or current-time dependencies.

The existing FITID policy also includes `Statement.bank_id` and `layout_id`.
Changing only the parser namespace can therefore change identifiers for identical
financial transactions and cause duplicate imports downstream. M6 adds a
regression demonstrating this risk; M7 also **does not change the algorithm**.
For a recognized historical layout, supplying identity context no longer
implicitly selects the generic parser. Callers that previously relied on that
behavior must pass their previous structural profile explicitly to retain that
parser namespace and its legacy FITIDs. Default CLI/GUI exports retain the
historical namespaces.
A separate migration milestone should define a versioned policy based on
stable account identity and normalized transactions, retain occurrence numbers
for legitimate duplicates, and verify specific/generic equivalence and collision
handling. It must preserve the legacy policy for previously exported accounts
until an explicit migration strategy and an actual downstream reimport test
establish how old/new identifiers are reconciled. No implicit migration or
automatic history persistence is introduced here.

`CREDIT` represents nonnegative amounts (including neutral zero), and `DEBIT`
represents negative amounts. The generic model cannot distinguish a payment
from another debit, so it does not infer `PAYMENT`. The complete normalized
description is exported once in `MEMO`. No `NAME`, `CHECKNUM` or `REFNUM` is
invented when the model has no corresponding information.

`OFXProfile` requires explicit bank/account IDs; only the Synthetic Bank path
can select the centralized fictitious profile automatically. Real-bank exports
require institution, branch and account metadata, reject known synthetic
placeholders and reject conflicts with the statement. Inter exports `FI/ORG`,
`FID` and `BRANCHID`. The branch check-digit separator is preserved, account
identifiers use digits, and leading zeroes are retained. Institution names come
from our explicit domain metadata rather than copying an official file's label.

The public reference at `tests/fixtures/ofx/reference.ofx` contains only fictitious
metadata and three cent-valued transactions with accents. Tests parse both the
generated OFX and the reference, compare structure without depending on indentation,
and check domain values independently. The official Inter file's reverse order,
opaque identifiers, extra transaction fields and inconsistent encoding are not
replicated. These choices still require an actual Athenas import acceptance test.

## M7 operator composition and migration

`generic/operators/` contains local capabilities rather than complete parsers:

| Capability | Responsibility | Output |
| --- | --- | --- |
| Scope segmentation | Partition retained rows and classify page frames | Financial scopes and informational regions |
| Transaction segmentation | Bind single-line/multiline and cross-page rows | Candidate segments before amount roles |
| Date attribution | Apply full-year individual/group dates and page context | Dated segments with source candidates |
| Amount roles | Select movement/running balance and declared control roles | Magnitude and monetary role candidates |
| Direction inference | Distinguish explicit signs, C/D and signed subtotals | Sign, evidence basis and source |
| Chronology inference | Check document calendar sequence without reordering | Declared/inferred economic order and evidence |
| Page continuation | Verify repeated headers, footers, notes and pagination | Retained rows |
| Financial evidence | Check only available balances, subtotals and totals | Immutable EvidenceReport |

Profile version 1 remains readable for M4/M5 payloads; no migration or automatic
storage is necessary. Description/frame options no longer require subtotal mode.
The currently proved unsigned-subtotal representation still requires grouped
dates and absent running balances. M8 uses bounded local hypotheses by default;
profile enumeration is retained only for regression oracles.
Use `infer_layout(document, legacy=True)` or
`GenericStatementParser().interpret(document, profile)` for the preserved legacy
path. Historical recognized grammars remain regression guards. After their successful
validation, the application prefers hypothesis composition, verifies semantic
equivalence and preserves their original FITID namespace. M8 combines the proved
header/summary bindings in one local operator; the older readers remain oracles.

Each transaction source exposes `date_source`, `description_sources`,
`amount_source`, optional `balance_source`, `direction_source` and its evidence
basis. References contain page, original visual row/nonblank text-line indexes,
word interval and region ID; they do not duplicate descriptions or values.
`pdf.sources.source_tokens(document, span, tolerances)` resolves a reference on
demand, using the same profile row tolerances. Opening/closing, daily balances
and totals remain traceable through monetary-role assignments.
`StatementAnalysis.financial_scope` exposes the single coherent financial region.
An explicit row partition can represent multiple financial scopes plus frames;
M7 does not automatically select or convert multiple accounts.

Ownership is inventoried before removing frames or selecting candidates. Missing,
duplicate, overlapping or out-of-scope monetary assignments prevent completion,
as do missing field sources, field/monetary role disagreements and descriptions
or dates overlapping monetary tokens. Every successful occurrence owns one
movement; every source running balance owns one corresponding balance. Monetary
content cannot be relegated to informational frames, and there is no IGNORE role.
Distinct surviving financial compositions yield AMBIGUOUS without scoring.

Regression tests compare both public families with legacy output, resolve all
fields, reuse wrapped descriptions/page continuity with signed amounts and C/D
markers, and check actual PDF/CLI/GUI/OFX behavior. Metamorphic recipes cover
horizontal/vertical translation, slight word spacing changes, 1% scaling and
moving page breaks before/after movements. Transactions and financial evidence
remain identical; changes beyond declared alignment/row tolerances abstain.

```bash
python -m pytest tests/generic/test_operators.py tests/generic/test_operator_metamorphic.py tests/ui/test_operator_composition.py -q
python tools/verify_m7_equivalence.py /local/first.pdf /local/second.pdf
```

The optional private verifier accepts paths supplied locally and checks 29/13
transactions, legacy/specific/composed equivalence, financial evidence, field
sources and deterministic OFX using independent fictitious export metadata.
It writes nothing and prints aggregates only. It never opens the third PDF.
The third document is reserved for a final `analyze_pdf` diagnostic probe; failure
results expose `blocking_capabilities` without source text. No rules were added
for that document. Athenas acceptance and FITID migration remain separate work.

## M8: constrained structural hypotheses

`generic/hypotheses.py` searches small local domains over the M7 operators. An
immutable `StructuralHypothesis` references one financial scope, segments, date
bindings, monetary roles, directions and economic chronology. `SearchBudget`
limits each segment to four date/amount combinations and the entire exploration
to 128 partial/complete states. Exceeding either limit yields `AMBIGUOUS`, even
when a plausible winner has already been found. No external solver, scoring,
network access or dependency was added.

The default path observes structural frames once, generates supported local date
and column alternatives, then extends one segment at a time. It verifies source
membership, exclusive ownership, period compatibility, column-role continuity,
subtotal signs/bounds, declared credit/debit bounds and complete financial links
before building a Statement. Only surviving complete candidates reach the shared
M7 materializer and full validation. Missing evidence does not fail a constraint.
A supplied `LayoutProfile` narrows dates, columns and frames; it cannot waive
ownership, provenance or financial reconciliation. Version-1 M4/M5 JSON continues
to load unchanged. Identity supplied in context is removed before search.

Chronology candidates can represent `ASCENDING`, `DESCENDING` and `UNKNOWN`
(the historical serialized `undeclared` value). Transactions retain document
order. When balance positions provide order-dependent controls, ascending and
descending hypotheses are tested with the same exact mathematics. Unknown order
cannot certify positional checkpoint links. Calendar evidence still constrains
monotonic economic dates; reversed layouts without verifiable order controls
remain unsupported. The fictitious reverse fixture deliberately uses identical
posting dates, so only balance mathematics selects descending order.

`BalanceCheckpoint.after` counts preceding movements in economic order. Opening,
running, daily, sparse and closing balances form boundaries; a link is verified
as soon as all movements between its endpoints are assigned. Exact Decimal
arithmetic checks `later - earlier = sum(interval)` and also rejects conflicting
balances at the same boundary. Standalone daily/sparse checkpoints carry PDF
references and exclusive monetary ownership. Missing per-transaction balances
stay absent. Detection currently covers only proved daily headings and an explicit
synthetic intermediate-balance label, not universal checkpoint recognition.

Material equivalence compares financial fields, economic ordering, controls,
coverage and original field tokens. Adjacent description spans split differently
but referencing the same tokens are equivalent. Distinct financial outcomes or
material origins never receive a score or first-candidate tie-break. A canonical
representation is chosen only among materially equivalent survivors. Changing
candidate enumeration order does not change the selected interpretation.

The minimum evidence policy accepts a verified independent control: opening plus
closing, at least one running/checkpoint interval, signed group subtotals, or both
declared credit and debit totals. A partial chain proves its checked intervals,
not unseen endpoints. A lone closing balance, dates, coverage or movement signs
alone do not suffice. Absent controls remain `NOT_AVAILABLE`/`NOT_APPLICABLE`
according to their existing evidence semantics. Accordingly, removing an opening
balance from a statement with reconciled signed subtotals no longer forces
abstention. Conflicting understood summary declarations now yield `INVALID`
rather than the legacy generic `UNSUPPORTED` classification.

Acceptance requires one material outcome, complete field provenance, complete
monetary coverage, every applicable hard constraint and independent evidence.
Surviving competing interpretations or exhausted search yield `AMBIGUOUS`.
Missing structural capacities yield `UNSUPPORTED`; understood contradictions
without a survivor yield `INVALID`. Failed branches retain only constraint names
and counts, not raw document contents. The old generic `financial_context` label
is replaced by specific period, balance-label, summary-value, unclassified-region
and adjustment diagnostics. `StatementAnalysis.candidate_hypotheses` counts full
assignments attempted; partial exploration and pruning counts are available in
`InferenceResult`.

Legacy `InterParser`, `parser.py` and `grouped.py` remain oracles. The new path
still reuses their small context model and observed frame/description-boundary
inference. Removing them requires further extraction of that frame observation
and a separately authorized FITID/recognized-layout migration. No UI redesign or
parsing logic was added to the UI; successful statements lacking account metadata
remain visible with export disabled.

```bash
python -m pytest -q
python -m pytest tests/generic/test_hypotheses.py tests/ui/test_hypothesis_states.py -q
python tools/verify_m7_equivalence.py /local/first.pdf /local/second.pdf
```

The aggregate-only private verifier compares legacy, M7 operators and M8
hypotheses, including the specific oracle for the first fixture, coverage, field
sources and deterministic OFX. The third PDF is reserved for a final diagnostic
probe, never for deriving new layout rules. Current limits include one financial
scope, at most two transaction monetary regions and bounded local alternatives;
there is no broad segmentation search or automatic multi-account conversion.

## M9: partial monetary role domains

Monetary discovery and ownership are separate. `VisualCoverage` inventories
original monetary occurrences before framing; `MonetaryDomain` retains the exact
source span, existing money-region candidate and a small tuple of `FinancialRole`
values. Producing a domain does not claim its source or supply a balance value to
the financial context. Only an assignment in a structural hypothesis does that.

The audit found two former `financial_region_unclassified` branches in the
context operator: incomplete financial tokens inside a declaration, and financial
content outside the transaction area with no recognized binding. These are not
necessarily monetary ambiguity. Incomplete tokens now report
`monetary_token_incomplete`; a valid occurrence without admissible roles reports
`monetary_role_domain_empty`. Neither is discarded or given arbitrary roles.

The deliberately narrow new domain is a complete, unqualified `Saldo` declaration
before transactions: opening or closing balance. Both are existing roles with a
known balance concept but an unresolved temporal boundary. An explicit opening
or closing label remains fixed. Unknown labels, unbound summaries, arbitrary
checkpoint/subtotal alternatives and incomplete monetary regions stay unsupported.
Existing coupled movement/running-balance alternatives continue through their
amount operator; selecting one column still determines exclusive ownership of
the other. No institutional identity, document-specific phrase, score or larger
search limit is involved.

| Stage | Responsibility and uncertainty |
| --- | --- |
| Monetary extraction and inventory | Discover complete regions and retain original source spans |
| Page/scope and segment operators | Reject removed financial content, unsupported boundaries or incomplete segments |
| Context operator | Bind explicit declarations; produce the supported unqualified-balance domain; diagnose malformed or unknown content |
| Amount/date/chronology operators | Produce existing local admissible alternatives |
| Hypothesis engine | Enumerate assignments under the existing budget and decide material uniqueness |
| Partial constraints | Check membership, ownership, geometry, dates and available financial relations before materialization |
| Materialization and coverage | Claim each occurrence exactly once and verify complete field provenance |

For k new unresolved balance declarations, the theoretical boundary assignment
factor is at most 2**k, in addition to existing transaction/chronology alternatives.
Each choice consumes the same global budget: four local alternatives and 128
partial/complete states. Conflicting boundary declarations and available balance
relations prune early. Exhaustion always abstains, including when a plausible
survivor has already been visited. Eight small synthetic domains exercise global
exhaustion without changing the budget.

`hypotheses.py` now delegates frame observation, chronology possibilities,
checkpoint construction and hard constraints. `operators/geometry.py` observes
structure, existing chronology/financial operators supply candidates, and
`generic/constraints.py` checks partial assignments. The engine chiefly combines
candidates, accounts for the budget, materializes survivors and decides uniqueness.
The legacy grammars and regression oracles remain available.

`InferenceDiagnostic`, also propagated by `StatementAnalysis`, distinguishes
`material_ambiguity`, `search_incomplete`, `capability_missing`,
`constraint_contradiction` and `insufficient_evidence`, without changing public
`AnalysisStatus`. Material uniqueness is checked across **all** surviving
interpretations before minimum evidence: a stronger financial report cannot rank
one still-plausible interpretation over another weaker one.

Financial-control independence is tested permanently. `ControlInterval` refers
to source occurrences and movement occurrences, not copied text. Reused control
sources, circular controls, repeated/overlapping movement sources, foreign scopes
and crossing/nested subtotal groups are rejected. Different printed controls
covering the same movement interval confirm one interval, never increase its
independent count. Checkpoint verification rejects reuse of one source at multiple
boundaries and deduplicates equal boundaries; two copies of an isolated balance
cannot manufacture a financial link. Every printed occurrence still requires its
own monetary ownership. Hierarchical subtotal interpretation remains unsupported.

```bash
python -m pytest tests/generic/test_monetary_domains.py -q
python -m pytest -q
python tools/verify_m7_equivalence.py /local/first.pdf /local/second.pdf
python -m compileall -q src
```

The new tests use only fictitious documents and include actual PDF extraction,
application export, typed diagnostics, partial pruning, all four statuses,
control overlap/circularity, role-order invariance and unchanged search budgets.
A new admissible balance domain can now resolve successfully, remain materially
ambiguous or fail all constraints. Existing M8 regression expectations remain
unchanged. The third private document is used only as a final diagnostic probe;
this capability is not a claim that its previous blocker was role ambiguity.

## M10–M12: relational roles, ordered controls and causal search traces

The governing rule remains **structure authorizes candidates; constraints
eliminate candidates**. Reconciliation never supplies a missing monetary role.
The M9 baseline of 635 tests remains covered without changing its expectations.

The pipeline now makes observation, candidate generation and ownership explicit:

```text
MoneyRegion + SourceSpan → MonetaryObservation → structural owners/relations
    → MonetaryDomain / coupled AmountRoles → StructuralHypothesis
    → partial constraints → materialization → validation + complete coverage
```

An isolated unlabelled cell can suggest a scope balance or intermediate
checkpoint only when at least two complete transaction segments demonstrate a
distinct recurring monetary column and an adjacent scope/date-group frontier.
These are structural candidates, not selected balances. The chosen economic
order must agree with the frontier, and every selected running balance must
occupy that same column. An interior cell without a representable group cut
remains unsupported. Unknown captions cannot borrow the relationship. No
universal money-to-all-roles fallback, institution rule or scoring exists.

`MonetaryDiagnostic` distinguishes missing owners, insufficient role evidence,
conflicting geometry, unsupported transaction geometry and unsupported control
structure. `RoleDecision` records the consulted generator, considered role,
admission/refusal rule and source witnesses using indexes only. Explicit summary
bindings also retain their label witnesses; local transaction candidates require
a date, a description containing something other than date tokens, and direction
evidence before expansion. Exact balance/subtotal captions cannot become
transactions merely because a value of zero would reconcile.

`ControlInterval` retains its source, role and movement occurrences and adds a
half-open economic interval, balance references and optional credit/debit member
indexes. Period controls can contain groups; explicitly represented subtotal
hierarchies are valid. Crossing subtotal groups, cycles, reused owners, invalid
anchors and foreign scope rows are rejected, including in partial assignments.
Sharing an opening balance as a reference does not claim it twice. Distinct
printed controls over the same movements corroborate one interval; they do not
increase independence. An isolated balance boundary supplies no verified link.
Set-only legacy callers remain conservative because they cannot demonstrate
containment. Generating hierarchical subtotal groups from new PDF structures
is still unsupported; ordered validation alone does not invent those groups.

`hypotheses.py` is approximately 170 lines and orchestrates domains, expansion,
constraints, materialization and material uniqueness. Geometry/frame observation
now belongs to `operators/geometry.py` and `operators/pages.py`; legacy entry
points delegate to these observations. The specific parsers and M4/M5/M7 oracles
remain available. `StatementContext` is still defined in the legacy parser module
as a shared value model; new inference does not call its legacy readers.

`ConstraintFacts` inventories read-only structural facts once per analysis.
Fixed/partial chronological contradictions, reserved source collisions, control
origin and unassigned crossing groups can be rejected before building a
Statement. `InferenceResult.search_trace` records parent states, source/role
choices, elimination constraints and surviving control intervals without text or
monetary values. Equivalent description span constructions are canonicalized.
Material ambiguity is resolved before the minimum-evidence policy; stronger
evidence cannot rank competing material interpretations.

Limits remain **four admissible local alternatives and 128 visited states**.
Local generation stops when an additional admissible choice exceeds its limit;
incomplete segments never spend that limit. Global exhaustion still blocks an
already visited survivor. Synthetic comparison against an isolated M9 snapshot
reduced a calendar-controlled example from 10 to 8 visited states and missing
descriptions from 6 to 0; the eight-domain exhaustion case remains at 128.

```bash
python -m pytest tests/generic/test_relational_roles.py tests/generic/test_control_topology.py tests/generic/test_search_discipline.py -q
python -m pytest -q
python tools/verify_m7_equivalence.py /local/first.pdf /local/second.pdf
python -m compileall -q src tools tests
git diff --check
```

The new positioned recipe `relational_balances/pages.json` and all added tests
contain fictitious data. Limits remain one financial scope, digitally generated
PDFs, the existing date/money vocabulary, and narrow transaction segmentation.
FITID, OFX encoding/format, UI and dependencies are unchanged. Athenas acceptance
is still a separate requirement.

## Private documents

Never commit real bank statements or upload confidential fixtures. Committed
fixtures must be synthetic. `tests/private_fixtures/` is ignored and local-only.
See [AGENTS.md](AGENTS.md) for the engineering and final-review contract.
