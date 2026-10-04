# Engineering contract

## Product purpose

Convert PDF bank statements into OFX for an accounting office whose downstream
workflow uses Athenas. Processing must be local and work offline: financial
documents are confidential. Athenas compatibility requires a future acceptance
test and must not be claimed from M0's synthetic example.

## Product principles

- Process financial document contents locally; conversion must not require
  external servers or third-party APIs.
- Prefer deterministic parsing to probabilistic guessing. Fail closed whenever
  financial data is ambiguous; never silently discard malformed transactions.
- Correctness matters more than conversion rate. Keep parsing and validation
  inspectable and testable; never invent or silently correct financial data.

## Architecture rules

- Keep the conversion core independent of the future GUI. UI code must never
  contain bank parsing logic.
- Isolate PDF-library calls behind our own extraction module and document model.
  Isolate each bank/layout parser from other parsers.
- Generate OFX from normalized domain models and validate before successful
  export. Use `Decimal` for monetary values, never binary floating point.
- Avoid unnecessary persistence; no database without a concrete requirement.
- No OCR until digitally generated PDFs work reliably. No premature frameworks
  or abstractions for hypothetical requirements.
- Keep dependencies minimal and justify additions.

## Privacy and logging rules

- Never commit real client bank statements or upload private financial fixtures.
- `tests/private_fixtures/` is local-only and must remain ignored by Git.
- Committed fixtures must contain synthetic/fictitious data only.
- Do not persist raw transaction contents in logs. Avoid printing transaction
  descriptions unless explicitly needed for local debugging.
- Error messages should identify the failure without reproducing document text.

## Testing rules

- Parser behavior must be testable without a GUI. Each supported bank/layout
  should have dedicated fixtures and regression tests.
- Add regression tests for parser bugs whenever practical.
- Test success and failure paths. Validation failures must prevent export.
- Verify M0 through the actual synthetic PDF, extraction, parsing, validation,
  and OFX generation, plus a CLI run.

## Development rules

- Python is the primary language; target Python >= 3.12 and use modern type hints.
- Favor straightforward Python and small domain-oriented functions/classes.
  Prefer the standard library when sufficient and use `pathlib` for paths.
- Never use `float` for money. Keep signed amounts: positive is inflow/credit,
  negative is outflow/debit.
- Do not introduce GUI code during M0. Do not add servers, telemetry, network
  calls, OCR, real bank parsers, or packaging for a desktop installer during M0.
- Do not automatically commit or push changes.

## Final task review requirement

Every completed Codex implementation task must end with a review explicitly
answering at least these five concrete questions:

1. What problem was identified or addressed?
2. What was changed to solve it?
3. Why was this approach chosen?
4. What tests or validations were performed? Include commands and outcomes.
5. What risks, limitations, or follow-up work remain?

Every completed task must end with one suggested Git commit message written in
English. Only suggest it; never commit automatically unless explicitly requested.
