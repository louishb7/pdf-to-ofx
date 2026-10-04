# PDF to OFX

An offline conversion core for an accounting office that receives PDF bank
statements and uses OFX in its Athenas workflow. Financial document contents are
processed locally, without external services, APIs, telemetry, or persistence
beyond the requested output file.

## Current status: M0, M1 and M2

M0 proves a synthetic PDF → normalized statement → validation → OFX pipeline.
M1 adds initial support for the **investigated Banco Inter digital layout**,
validated locally against one private PDF and its corresponding official OFX.
This is not general support for every Banco Inter statement. Only digital PDFs
with extractable text are supported; pages without usable text fail explicitly.
Malformed rows, unsupported layout variations and balance mismatches stop
conversion. There is no GUI or OCR; processing remains entirely local.
M2 hardens deterministic OFX export and tests its financial semantics against
a small, fully fictitious public reference.

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
pdf-to-ofx tests/fixtures/synthetic/statement.pdf -o /tmp/synthetic-statement.ofx
pdf-to-ofx tests/fixtures/inter/statement.pdf -o /tmp/fictitious-inter.ofx
```

Dependency installation needs package access; conversion itself works offline.
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
The only direct runtime dependency is pdfplumber, isolated in the extraction
module. A normal virtual environment and pip are sufficient.

The public Inter fixture is entirely fictitious: it exercises multiple dates,
same-day credits/debits, running balances and a date heading at the end of page
one whose first transaction appears on page two. Private files are never needed
by the automated suite.

## Architecture

`application.convert.convert_pdf(Path(...))` is the GUI-independent entry point:

```text
Local PDF → extracted pages → deterministic layout detection → layout parser
          → immutable Statement/Transaction → validation → provisional OFX
```

Bank parsers receive our own document model. The OFX generator receives domain
values and also validates direct callers. Monetary values use `Decimal`, positive
for credits and negative for debits, with exact cent reconciliation. Normalized
transactions retain document order for balance validation; export sorts a copy.
Unsupported text is rejected rather than ignored.

Inter preserves the balance after each transaction and checks every adjacent
balance, each daily closing balance and the statement closing balance. Its
opening balance remains `None` when undeclared. This verifies the visible chain,
not a complete reconciliation against an independently supplied opening balance;
the first transaction cannot be checked arithmetically against an absent value.
Detection requires the institution/account header and the known layout markers;
an institution name mentioned in a transaction is insufficient.

The CLI publishes complete exports without replacing existing files using a
temporary file and a hard link in the destination directory. Filesystems without
hard-link support fail explicitly; output permissions are owner-only on POSIX.

OFX format choices and metadata live in `ofx/generator.py`. The output remains
OFX 1.02 SGML with closed tags, declared `USASCII` / `CHARSET:NONE` and actual
ASCII bytes. Accents are preserved through numeric character references. The
CLI uses this same encoding. Dates use `YYYYMMDD`, without invented times or
timezones; `DTSERVER` and `DTASOF` use the statement end date.

Export order is ascending calendar date, then description (Unicode lexical
order), then signed amount. Source chronology and running balances must pass
validation before sorting; invalid source order is still rejected. FITIDs use
SHA-256 of normalized transaction/account identity plus an occurrence number
for identical duplicates. Duplicate transactions remain present with unique,
stable IDs; an identifier collision fails explicitly. Sorting does not change
transaction identity. Monetary values have two decimal places; signed zero is
canonicalized to `0.00`, including in FITID identity. Identical normalized inputs
produce identical bytes without randomness or current-time dependencies.

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

## Private documents

Never commit real bank statements or upload confidential fixtures. Committed
fixtures must be synthetic. `tests/private_fixtures/` is ignored and local-only.
See [AGENTS.md](AGENTS.md) for the engineering and final-review contract.
