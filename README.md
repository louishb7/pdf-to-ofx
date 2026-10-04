# PDF to OFX

An offline conversion core for an accounting office that receives PDF bank
statements and uses OFX in its Athenas workflow. Financial document contents are
processed locally, without external services, APIs, telemetry, or persistence
beyond the requested output file.

## Current status: M0, M1, M2 and M3

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
4. Click **Salvar OFX** and choose a new destination using the system dialog.
   The suggested filename follows the PDF name; existing files are refused.

OFX contents remain in memory until saving is requested. Opening a new PDF clears
the previous result immediately, including after a failed conversion; an old OFX
cannot remain exportable. Cancelling a dialog makes no changes. Missing balances
are displayed as **Não informado pelo extrato**. Conversion failures appear in
Portuguese without document contents or tracebacks.

The GUI and CLI call the same application services. Processing is local and
does not send statements over the internet. There is no conversion history,
automatic financial persistence, telemetry, editing, or persistent settings.
Only the investigated Inter digital layout and the Synthetic Bank demonstration
are supported. **Compatibilidade com Athenas ainda não validada.**

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

The shared `application.export.write_ofx` service publishes complete exports
for both CLI and GUI without replacing existing files using a
temporary file and a hard link in the destination directory. Filesystems without
hard-link support fail explicitly; output permissions are owner-only on POSIX.

The `ui/` package owns only the window, presentation and desktop entry points.
It calls `application.convert.convert_pdf` and `application.export.write_ofx`;
bank parsing, PDF extraction, financial validation and OFX generation remain in
the existing core. The transaction table follows document order for reviewing
running balances, while the export retains M2's canonical ordering.

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
