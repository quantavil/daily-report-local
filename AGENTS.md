# AGENTS.md — daily-report-local

## Purpose and status

Fill `Daily Working Report.xlsx` from ICEGATE Gmail PDFs using deterministic local
parsing. Prefilled Excel rows provide bill identity and processing scope.
This document describes the implemented local parser and its maintenance contract.

## Design discipline

- No AI extraction or fallback. Remove AI integrations, prompts, schemas, caches,
  usage reporting, key requirements, and dependencies when replacing their consumers.
- One implementation per responsibility. Delete replaced code, dead helpers,
  obsolete tests, unused dependencies, and outdated documentation. No parallel
  legacy pipeline or compatibility-only modules. Root `sync.py` is only a launcher.
- All executable ICEGATE-specific rules belong in `src/daily_report/rules.py`:
  filename structure, codes, field ownership, pages, labels/header variants,
  layout relationships, validation, and revision policy.
- Update this document alongside implemented rules and boundaries. Document the
  contract here; do not duplicate executable policy across Python modules.
- Prefer label-anchored, layout-aware parsing. Regex can recognize labels and
  validate tokens; it must not choose arbitrary nearby names, dates, or numbers.
- Avoid speculative frameworks, abstractions, and one-class-per-stage structure.

## Authoritative field sources

Expected receiving sequence: SB21 → SB22 → SB35. Process available documents
independently; missing earlier documents do not block supported later fields.

| Document | Owned fields and eligible source | Original pages |
| --- | --- | --- |
| SB21 — First Copy | True exporter from `1. EXPORTER'S NAME & ADDRESS`; primary invoice token from `2. INVOICE No. & Dt.` | 1–2 |
| SB22 — LEO Copy | LEO from `6. LEO Date` | 1 |
| SB35 — eGatepass | Movement-table container, size, seal: `11. Container`, `14. Size`, `12. Seal` | All relevant pages and continuations |

- Never substitute the CHA company for the exporter or include address details.
- Invoice excludes its associated date, amount, and currency.
- No SB22/Gatepass exporter or invoice fallback; no Gatepass order-date LEO fallback.
- Never derive LEO from filename document date, SB date, or email receipt time.
- Filled Excel cells are not source evidence. Previously AI-filled values require
  independent PDF verification; matching Excel alone does not validate extraction.
  Assessment dates never substitute for LEO. Unsupported new fields remain blank.
- Before updating part of an existing container triplet, require every retained member
  to match the selected ordered source records. Conflicts block the whole triplet.
- Keep container, size, and seal as ordered records, preserving missing positions.
  Serialize aligned newline-separated Excel columns only at the write boundary.
  Never independently compact or deduplicate those columns. Verified blank positions
  may accompany accepted partial records, but their row remains Partial. Container
  field outcomes carry `value_type="records"` and evidence, never parallel value lists.

## Identity and Gmail discovery

- Apply row/SB scope before validating row values. Detect cancellation before parsing
  dates or ports so cancelled data cannot block extraction. Invalid identity values
  produce row-specific errors and no Gmail calls for that row, without blocking others.
  Preserve invalid existing LEO text for inspection/forced repair; do not reinterpret
  it as identity or infer cancellation outside Billing Status.
- Read SB number, SB date, port, existing values, and any explicitly supplied document
  date from selected rows. Do not assume a document date column exists.
- Search only emails from `noreply@icegate.gov.in`; validate sender metadata too.
  Gmail remains read-only. No fallback across other senders, forwarded mail, Spam, or Trash.
- Canonical filename:
  `<SB:7><SB date:DDMMYYYY><port:6><code:SB21|SB22|SB35><document date:DDMMYYYY><time:HHMM>.pdf`.
- Parse fixed segments; validate dates, time, and code. Reject malformed or mismatching
  identities rather than guessing components.
- Generate the longest prefix supported by known row data, e.g.
  `754640407102026INMBD6SB22`. Append `08102026` only when document date is supplied.
  Never infer document date/time from receiving sequence.
- Gmail filename queries do not reliably match partial canonical names. Search the
  approved sender using the SB number and the generated indexed subject token
  `No<SB>Dt<DDMMYYYY>`, then apply filename prefixes and full identity validation
  locally. Without a known SB date, discover within the same sender and filter
  attachment identity locally; never silently treat an incomplete query as absence.
  Paginate and inspect attachments; subjects alone never establish a match.
- Bill identity is SB number + SB date + port. Unknown components remain unknown;
  conflicts must not be combined automatically. Verify PDF identity before parsing.
- Use stable message ID + MIME part ID + filename for attachment reuse. Gmail
  attachment download tokens can rotate between reads; never use them as persistent
  identity. Missing MIME part IDs receive stable traversal ordinals. Validate retained
  PDF hashes and any supplied fresh inline bytes before reuse.
- At or after SB date + 30 days, skip Gmail only when all selected rows for the bill
  have actual Status `Success` and fresh native parsing of all three retained PDFs accepts
  every required owned field with complete aligned container records and no issues/
  runtime failures. Excel Success alone, saved values, and previously AI-filled cells
  never establish verified Success.
- Partial, Not found, and Error rows continue discovery regardless of age, even when
  complete local source proof exists (for example, from read-only verification).
  Successful batches younger than 30 days still check revisions. Unknown SB dates
  cannot establish a cutoff and continue discovery. `--force`/`--refresh` preserve
  this policy; Billing Status `CANCELLED` always skips discovery regardless of age.
  Never infer cancellation from Status `Cancelled` alone.
- During discovery, select the newest canonical filename timestamp per document type.
  Equal timestamps with different content are ambiguous. Retain only the newest
  SB21, SB22, and SB35 per batch, at most three PDFs.
- A failed newest download blocks that document type during live checking; preserve
  previous local bytes but never silently fall back to them. Safely retain the newer
  original before removing the obsolete PDF and matching processed JSON/MD.
- Preserve newest-observed candidates in `result.json` `selected_sources` across scoped
  runs, including failed downloads; `selected_hashes` holds only validated retained
  bytes. Local-only runs never fall back to an older PDF after a known newer failure.
- Local-only successful bills parse saved originals/native layout afresh. Missing or
  damaged originals prevent verified Success and require discovery. Failures remain
  isolated by document type: damaged SB21 does not block valid SB22/SB35. Missing
  matching documents remain pending; never substitute another source.

## SB working records and reuse

```text
data/2026/754640407102026INMBD6/
├── pdfs/
│   └── <canonical filename>.pdf
├── processed/
│   ├── <same filename stem>.json
│   └── <same filename stem>.md
└── result.json
```

- Year folders use the filename shipping-bill calendar year, never document date:
  December SB/January LEO remains in the SB year. Unknown SB dates use `unknown` year.
  Batch names are SB number + SB date + port, generated by rules. Filename metadata
  resolves document batches; unknown identity components use literal `unknown` in
  pending folders. Never merge conflicting identities or silently complete workbook identity.
- Preserve original bytes and canonical filenames, which are expected to be unique.
  Unexpected same-name/different-content attachments report Error and preserve the
  original; no collision suffixes. Restore corrupt retained PDFs only when downloaded
  bytes match the recorded hash; damaged originals cannot qualify for local-only Success.
- Each processed JSON holds `metadata`, `content_hash`, attachment `sources`, `layout`,
  `layout_versions`, `fields`, `containers`, and `parser_version` at the top level.
  Batch `result.json` retains selected source metadata/hashes and workbook identity.
  No manifest, hash folders, separate layout/parsed files, or cache envelopes.
- Reuse unchanged PDFs and native layouts. PDF changes invalidate layout; rules/layout
  profile/PyMuPDF changes invalidate layout reuse. Every run parses native layout once;
  saved business values are inspection output, never reused for extraction. Markdown
  is rendered from the same native cells, with no second converter.
- Keep workbook-provided identity, filename metadata, and PDF-derived fields separate
  with provenance. Flag disagreements rather than silently choosing one.
- Persist accepted, verified-blank, and rejected outcomes distinctly. Runtime failures
  are retryable failures, never verified blanks or permanent completed results.
- Use atomic artifact writes and coordinate workers targeting the same SB folder.
  Persist document results before Excel updates so interrupted runs can reuse work.
- Business artifact directories/files use 0700/0600 permissions and remain git-ignored.

## Code boundaries

Package: `src/daily_report/`. Split modules further only for demonstrated need.

| Module | Responsibility and boundary |
| --- | --- |
| `cli.py` | Arguments/output; no parsing or workbook mutation logic |
| `workflow.py` | Coordinate discovery, reuse, parsing, validation, updates; no labels/patterns |
| `rules.py` | All ICEGATE-specific policy; no Gmail/filesystem/Excel side effects |
| `models.py` | Typed identities, metadata, evidence, outcomes, container records; no service calls |
| `gmail.py` | Metadata/downloads; no field extraction or Excel access |
| `documents.py` | Native layout and generic parsing driven by rules; no Gmail/Excel access |
| `storage.py` | Document artifacts, reuse, atomic persistence; no field inference/Excel writes |
| `workbook.py` | Row scope, safe updates, backups/saves; accepts validated results, never parses PDFs |

## Pipeline and validation

Select rows → verify local Success/age → discover attachments or use local originals
→ validate filenames/select newest → retain up to three PDFs → reuse or extract layout
→ parse/validate → persist results/evidence
→ build permitted updates → write sequentially → backup/save.

- Check native identity-bearing content across all PDF pages, including pages outside
  field ownership; contradictory identities reject the document. Preserve original
  page numbers, native block order, text, and coordinates.
  Never use `page.get_text(sort=True)`; close PDF documents in `finally` blocks.
- Reconstruct split neighboring headings before bounding a cell; never allow CHA
  text to bleed into exporter evidence. Reject address-only exporter candidates and
  date-only invoice cells. Supported exporter shapes and address exclusions live in rules.
- Associate identity components with the bill header band or the explicit supported
  Gatepass secondary-header profile, not an unrelated port/date label elsewhere.
- Flat and stacked movement records require supported header/continuation geometry.
  Unsupported continuations reject rather than silently return a truncated list.
- Parse label/cell/bounded-region relationships. Markdown is for inspection, not
  the sole source for interpreting table geometry.
- Native PyMuPDF extraction only; OCR is disabled. Unreadable, encrypted, scanned,
  and unsupported-layout documents yield explicit unresolved/error outcomes.
- Each accepted value carries PDF identity/hash, page, eligible label/header,
  source text, and location. Validate field ownership and source association.
- Distinguish accepted, verified blank, rejected/ambiguous, missing document, runtime
  failure. Never guess values merely to complete fields.
- Use representative anonymized fixtures for layout variants and continuation tables.
  Test correct source association, not merely successful token recognition.

## Workbook and CLI safeguards

- Preserve supported commands and row/SB/sheet scope. Remove/revise obsolete option
  semantics explicitly; retain no AI-specific behaviour.
- `--check`: read-only, no Gmail calls. Verification: read-only fresh local parsing.
  Verification reuses layout and parses afresh without workbook writes. `--refresh`
  re-extracts native layout from stored PDFs without forcing download; `--no-cache` is removed.
- Normal sync checks filled incomplete/unverified rows too. Preserve their values
  unless forced; mismatching filled values or missing/rejected selected fields prevent
  Success. Reuse fresh source parsing within a run, never saved business values.
- `--force`: permits validated updates to selected filled fields. `--leo-only`: LEO
  data writes only. Status writes obey row/SB filters. Reject unknown sheet names.
- Preserve formulas and existing values on blank/missing/rejected/conflicting/failed
  evidence. Never erase an existing date during routine sync. An explicit user
  correction may clear a confirmed incorrect value, with backup and scoped
  verification. Store LEO as native `datetime.date`.
- Never use `openpyxl.load_workbook(read_only=True)`.
- Resolve merged cells to top-left parents; flag conflicting updates to shared parents
  instead of allowing worker completion order to choose a value. Merged data cells
  touching cancelled rows are protected even when those rows are selected. A blocked
  container member blocks the entire proposed triplet; do not write misaligned remnants.
- Skip `Billing Status == CANCELLED`; stamp selected rows `Cancelled`.
- Excel writes are sequential. Fetch/parse concurrency is bounded at eight workers;
  Gmail services are thread-local and authentication refresh is protected.
- Before overwrite, create `.bak-<YYYYMMDD-HHMMSS-ffffff>.xlsx`; retain newest five.
- FY sheets use `Status`: `Success`, `Partial`, `Cancelled`, `Not found`, `Error`.
  Success requires required-field completeness and no unresolved selected-field validation
  failure. Error means runtime/extraction failure, not simply missing documents.

```bash
uv run --no-sync sync.py --check
uv run --no-sync sync.py --sb 7546404
uv run --no-sync sync.py --rows 1349-1351
uv run --no-sync sync.py --verify-rows 2-20 --leo-only
uv run --no-sync pytest -q
uv run --no-sync ruff check src/ tests/ sync.py
```

## Privacy and completion

- Git-ignore `data/`, private caches, source PDFs, processed business artifacts, `.env`,
  `credentials.json`, `token.json`, backups, and all other `*.xlsx` files.
- Root `Daily Working Report.xlsx` is the sole workbook approved for tracking/publication.
- Never commit live business documents or secrets. Fixtures must be synthetic/anonymized.
- Finish replacements by deleting superseded code and updating tests, dependencies,
  README, and this contract. Run relevant checks before claiming completion.
- Keep current setup/usage in README and maintenance rules here. Remove completed
  implementation ledgers and unused legacy caches. Build outputs, generated package
  metadata, bytecode, and test/lint caches are disposable; never delete reusable
  `data/` records or the active environment as part of routine clutter cleanup.
- Documentation changes alone do not complete the codebase redesign.
