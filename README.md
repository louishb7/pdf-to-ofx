# PDF to OFX

An offline conversion core for an accounting office that receives PDF bank
statements and uses OFX in its Athenas workflow. Financial document contents are
processed locally, without external services, APIs, telemetry, or persistence
beyond the requested output file.

## Current status: M0 and M1

M0 proves a synthetic PDF → normalized statement → validation → OFX pipeline.
M1 adds initial support for the **investigated Banco Inter digital layout**,
validated locally against one private PDF and its corresponding official OFX.
This is not general support for every Banco Inter statement. Only digital PDFs
with extractable text are supported; pages without usable text fail explicitly.
Malformed rows, unsupported layout variations and balance mismatches stop
conversion. There is no GUI or OCR; processing remains entirely local.

**Athenas compatibility has not been validated.** The provisional OFX 1.02
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
for credits and negative for debits, with exact cent reconciliation. Transactions
retain document order; unsupported text is rejected rather than ignored.

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

OFX format choices and metadata live in `ofx/generator.py`. FITIDs are derived
from normalized transaction/account data and an occurrence number for identical
duplicates. Identical inputs produce identical exports. Dates are emitted at
midnight without a timezone; `DTSERVER` uses the statement end date. These are
provisional choices awaiting compatibility tests, not asserted bank identities.
Inter also exports `FI/ORG`, `FID` and `BRANCHID`. The branch check-digit separator
is preserved, account identifiers use digits, and leading zeroes are retained.
The official file's date/type/identifier peculiarities are not replicated: we
retain midnight dates, `CREDIT`/`DEBIT`, source descriptions in `MEMO`, and hashed
FITIDs including branch identity and duplicate occurrence numbers. We do not
invent `NAME`, `CHECKNUM` or `REFNUM`, or reproduce inconsistent encoding headers.

## Private documents

Never commit real bank statements or upload confidential fixtures. Committed
fixtures must be synthetic. `tests/private_fixtures/` is ignored and local-only.
See [AGENTS.md](AGENTS.md) for the engineering and final-review contract.
