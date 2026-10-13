# Local ICEGATE Parser Rebuild Implementation Plan

> **For agentic workers:** Use subagent-driven-development with independent file ownership and integrated review. The user approved execution and explicitly requested parallel agents.

**Goal:** Replace the AI pipeline with deterministic, source-bound PDF parsing and reusable SB folders.
**Architecture:** Small `daily_report` package; a single rules file drives generic PDF mechanics. Gmail and storage own discovery/artifacts; workbook owns safe mutation; workflow coordinates typed results.
**Tech Stack:** Python 3.14, Pydantic v2, PyMuPDF, AnyDoc native Markdown, openpyxl, Google Gmail SDK, Typer.
**Spec:** `AGENTS.md` is the authoritative design contract.

## Global constraints

No AI or legacy pipeline. Sender only noreply@icegate.gov.in. SB21 exporter/invoice pages 1–2; SB22 LEO page 1; SB35 movement-table records across relevant pages. Preserve formulas, row scope, merged-parent conflicts, native dates and backups. Private data stays untracked. No live workbook mutation during development.

## Review focus

- Ambiguous geometry or duplicate labels must reject, not guess.
- Filename date/time ties with different bytes must reject.
- Same SB with different date/port must never share results.
- Merged-parent conflicts must preserve the original cell regardless of completion order.
- Corrupt/version-stale artifacts must be regenerated, while API/storage failures remain errors.

## Shared interfaces

Models (root-owned): `BillIdentity`, `DocumentMetadata`, `Attachment`, `Evidence`, `FieldResult`, `ContainerRecord`, `LayoutSpan`, `LayoutPage`, `DocumentLayout`, `ParsedDocument`, `BillResult`, `WorkbookRow`, `RowUpdate`.

Rules/parser task exposes:
- `parse_filename(filename: str) -> DocumentMetadata` (raises ValueError).
- `filename_prefix(identity: BillIdentity, code: str) -> str`.
- `matches_identity(expected: BillIdentity, actual: BillIdentity) -> bool`.
- `document_codes(fields: set[str]) -> set[str]`.
- `extract_layout(pdf: bytes, metadata: DocumentMetadata) -> DocumentLayout`.
- `parse_document(layout: DocumentLayout, metadata: DocumentMetadata, content_hash: str) -> ParsedDocument`.
- `render_markdown(pdf: bytes, metadata: DocumentMetadata) -> str`.
- Constants `DATA_FIELDS`, `FIELD_DOCUMENTS`, `RULES_VERSION`, `PARSER_VERSION`, `LAYOUT_VERSION`, `EXCEL_COLUMNS`, `SENDER`.

Gmail/storage task exposes:
- `GmailClient(credentials_file: Path, token_file: Path, service=None)`, `.authenticate()`, `.discover(identity: BillIdentity, codes: set[str]) -> list[Attachment]`, `.download(attachment: Attachment) -> bytes`.
- `BillStorage(root: Path, identity: BillIdentity)`, `.get_pdf(attachment) -> bytes | None`, `.put_pdf(attachment, raw) -> str` (content hash), `.get_layout(hash)`, `.put_layout(hash, layout)`, `.get_parsed(hash)`, `.put_processed(hash, parsed, markdown)`, `.put_result(result)`.
- Version checks are implemented by storage; refresh policy is workflow-owned. No module imports workflow/CLI/workbook.

Workbook task exposes:
- `WorkbookStore(path: Path)`, `.rows(sheet: str) -> list[WorkbookRow]`, `.apply(sheet: str, updates: list[RowUpdate], force=False, fields=None) -> dict[int, list[str]]`, `.save()`.
- `WorkbookRow(row_idx, identity, values, cancelled)`; `RowUpdate(row_idx, values, status, detail)`.
- `apply` preflights merged-parent conflicts and flags affected statuses Partial; retains formulas, nonempty cells unless force, and missing/rejected values always.

## Task 1: Models and plan (root)
- [x] Write model tests for validated bill identity, typed field states and evidence, container alignment.
- [x] Run tests to confirm missing implementation, then implement models and verify.
- [x] Freeze shared contracts before dispatching parallel tasks.

## Task 2: Deterministic rules and documents (parser agent)
Files: `rules.py`, `documents.py`, `tests/test_documents.py`, `tests/test_rules.py`.
- [x] Inspect representative local PDFs privately; no fixtures with live business data.
- [x] Write failing synthetic layout/PDF tests: canonical names, invalid dates, wrong-source dates, label association, split headings, movement continuations, missing positions, ambiguous geometry and conflicting PDF identity.
- [x] Implement centralized rules and geometry-driven parsing with evidence and native conversion.
- [x] Verify focused tests and lint; report supported layouts and unresolved limitations.

## Task 3: Sender-scoped discovery and SB artifacts (storage agent)
Files: `gmail.py`, `storage.py`, `tests/test_discovery.py`, `tests/test_storage.py`.
- [x] Write failing tests: sender spoof/mismatch, pagination, nested/inline attachments, known prefix filters, content collisions, identity isolation, corruption, version invalidation, private permissions.
- [x] Implement discover/download and atomic versioned persistent artifacts; retain revisions.
- [x] Verify focused tests and lint.

## Task 4: Workbook safety (workbook agent)
Files: `workbook.py`, `tests/test_workbook.py`.
- [x] Write failing tests: exact sheet/filter scope, dates and port identity, cancelled rows, formulas, existing dates, merged parents, conflicting shared-parent updates, backups and atomic save.
- [x] Implement focused workbook adapter driven by typed values; do not parse PDFs.
- [x] Verify focused tests and lint.

## Task 5: Workflow and CLI (root)
Files: `workflow.py`, `cli.py`, `__init__.py`, root `sync.py`, `tests/test_workflow.py`, `tests/test_cli.py`.
- [x] Write failing integration tests: phased arrival, document ownership, timestamp revisions/ties, identity mismatch, reuse/refresh, read-only verification, force/LEO scope and status.
- [x] Implement workflow with fresh discovery, bounded workers, coordinated storage and sequential workbook writes.
- [x] Wire CLI and environment configuration; obsolete AI cache flags become local rebuild/reuse controls.
- [x] Verify integration tests against synthetic PDFs/workbooks and mock Gmail.

## Task 6: Cleanup, integration review, verification (root + reviewer)
Files: pyproject.toml, uv.lock, .env.example, .gitignore, README.md, AGENTS.md; delete superseded flat modules/tests and obsolete AI design docs.
- [x] Remove obsolete source/tests only after replacement safety coverage exists.
- [x] Update packaging/dependencies and documentation; keep live/private artifacts untouched.
- [x] Independently review every agent diff and fix verified defects.
- [x] Run full pytest, Ruff, packaging/CLI smoke and read-only workbook check.
- [x] Run independent whole-change review, fix findings, repeat affected checks.

## Execution ledger

- User explicitly authorized implementation and parallel agents; continue without another approval round.
- Work is on branch `rebuild/local-icegate-parser` in the shared checkout to keep private local PDFs available for inspection. No push, merge, or live workbook writes.
- Current baseline: 202 tests passed; Ruff clean before the rewrite.

- Completed parser, discovery/storage, workbook and root integration tasks; independent reviews found defects which were reproduced and fixed with regression tests.
- Live Gmail filename-prefix and bare-SB queries were incomplete. Exact generated subject identity tokens now discover all three source types within the approved sender.
- Live native reference bill validates all six fields and one movement record; business artifacts remain ignored and the workbook is unchanged.
- Gmail download IDs rotate. Persistent attachment identity now uses message/MIME part/filename; repeat-run download/conversion reuse is verified separately.
- Final replacement suite: 139 tests pass; Ruff clean. Package build and CLI help/read-only audit verified.
