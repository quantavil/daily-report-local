# AGENTS.md — daily-report-local

Fills `Daily Working Report.xlsx` from ICEGATE Gmail PDFs by deterministic local parsing;
prefilled rows give bill identity and scope. This is the maintenance contract; setup
and usage live in `README.md`.

## Toolchain
- Bun only: `bun install`, `bun run sync ...`, `bun run dev`, `bun test`, `bun check`.
- Code in `src/ts/`, tests in `tests-ts/`. Split modules only for demonstrated need.
- No AI, no OCR: unreadable, scanned or unsupported PDFs yield explicit rejected/error outcomes.
- `mupdf` is AGPL-3.0: fine for private local use; switch to `pdfjs-dist` if distributed.

## Design discipline

- One implementation per responsibility. Delete replaced code, dead helpers, unused
  dependencies, obsolete tests and stale docs. No compatibility shims.
- All executable ICEGATE policy (filenames, codes, field ownership, pages, labels,
  geometry tolerances, validation, versions, `result_is_success`) lives in
  `src/ts/rules.ts`. Other modules and this file must not restate it.
- Parse by label-anchored layout. Regex may recognize labels and validate tokens; it
  must not pick arbitrary nearby names, dates or numbers.

| Module | Responsibility and boundary |
| --- | --- |
| `cli.ts` | Argument parsing, output, exit status; no parsing or workbook logic |
| `workflow.ts` | Orchestrates skip, discovery, reuse, parsing, `make_update`, write; no labels or patterns |
| `rules.ts` | All ICEGATE policy; no Gmail, filesystem or Excel side effects |
| `models.ts` | Typed identities, metadata, evidence, field outcomes, container records |
| `gmail.ts` | Read-only discovery and downloads through the `gws` CLI; no extraction, no Excel |
| `documents.ts` | Native layout via mupdf and rule-driven parsing; no Gmail, no Excel |
| `storage.ts` | Per-bill artifacts, reuse, locking, atomic writes; no field inference, no Excel |
| `workbook.ts` | Row scope, guarded updates, hash check, backups; takes validated results, never parses PDFs |
| `ooxml.ts` | String-level xlsx patching on fflate; no business rules |
| `server.ts` | `bun run dev` localhost dashboard (`src/ui/index.html`): spawns the CLI as a child and serves its output; no parsing or workbook logic |

## Authoritative field sources

Documents arrive SB21 → SB22 → SB35 and are processed independently; a missing one
never blocks fields owned by another.

| Document | Owned fields and eligible source | Pages |
| --- | --- | --- |
| SB21 First Copy | Exporter from `1. EXPORTER'S NAME & ADDRESS`; primary invoice token from `2. INVOICE No. & Dt.` | 1–2 |
| SB22 LEO Copy | LEO date from `6. LEO Date` | 1 |
| SB35 eGatepass | Movement table: `11. Container`, `14. Size`, `12. Seal` | all, incl. continuations |

- Exporter excludes the CHA company and address lines; invoice excludes date, amount,
  currency. No cross-document fallback, and never derive LEO from filename date, SB
  date, assessment/order date or email time.
- Excel cells are never source evidence. Unsupported new fields stay blank.
- Container, size and seal are ordered records with preserved empty positions, joined
  into aligned newline-separated columns only at the write boundary, never compacted
  or deduplicated. Their outcomes use `value_type="records"` plus evidence. Verified
  blank positions may accompany accepted records but the row stays Partial.
- Updating part of a container triplet requires every retained member to match the
  selected source records; any conflict or blocked member blocks the whole triplet.

## Identity and Gmail discovery

- Bill identity is SB number + SB date + port. Unknown components stay unknown;
  conflicting candidates are never merged (the row becomes Partial; fill date and port).
- Apply row/SB scope first and detect cancellation before parsing dates or ports.
  Invalid identity yields a row-specific Error, no Gmail calls, and blocks no other row.
- Canonical filename: `<SB:7><SB date:DDMMYYYY><port:6><SB21|SB22|SB35><doc date:DDMMYYYY><HHMM>.pdf`;
  reject malformed names, never guess components.
- Only mail from `rules.SENDER`: exactly one `From`, no forwarding headers, not Spam
  or Trash. No fallback to other senders.
- Gmail filename search is unreliable for partial names. Query the sender plus the SB
  number and `No<SB>Dt<DDMMYYYY>` terms (sender only when the SB date is unknown),
  paginate, then filter attachments by prefix and full identity locally. Subjects never
  establish a match; failed access is an error, never "no messages".
- Gmail goes through `gws` using the existing `gws auth login`; `GWS_BIN` overrides the
  binary, otherwise `PATH`, then `~/.bun/bin/gws`.
- Attachment identity is message ID + MIME part ID (traversal ordinal when absent) +
  filename; rotating download tokens are never identity.
- Per document type, the newest filename timestamp wins; equal timestamps with
  different bytes are ambiguous and rejected. At most three PDFs (newest SB21, SB22,
  SB35) are retained per bill.
- A failed newest download blocks that type; older local bytes are never a silent
  fallback. `result.json` `selected_sources` keeps the newest observed candidates across
  scoped runs; `selected_hashes` holds only validated retained bytes.

## Run semantics

- Normal run: Status `Success` rows are skipped before any auth, Gmail, parse or write;
  all other rows continue discovery. Gmail login happens only when non-skipped work
  needs it, and a login failure stops the run before any workbook or artifact change.
- `--force` includes Success rows, checks Gmail for revised PDFs, and permits validated
  repair of filled cells. Otherwise filled cells are kept and a differing one prevents
  Success.
- `--check` and `--verify-rows` are strictly read-only: no Gmail, locks, directories or
  workbook write. `--check` lists missing fields. `--verify-rows` re-parses saved
  originals afresh and compares with Excel; missing or damaged originals need discovery.
- `-n/--dry-run` does everything except save the workbook. `--refresh` re-extracts
  layout from stored PDFs without forcing a download.
- Billing Status `CANCELLED` rows never trigger Gmail and are stamped `Cancelled`;
  never infer cancellation from Status alone.
- Status values: `Success`, `Partial`, `Cancelled`, `Not found` (no matching document),
  `Error` (runtime or extraction failure).
- Success is terminal and gated by `rules.result_is_success` (all six fields accepted
  from consistent sources with matching evidence, complete container records).
  Partial-field runs such as `--leo-only` can never yield Success.
- Nothing found writes no `result.json` and creates no directories.

## Per-bill records

```text
data/<SB year>/<SB:7><SB date:DDMMYYYY><port:6>/
├── pdfs/<canonical filename>.pdf
├── processed/<same stem>.json   # + .md rendered from the same native layout
└── result.json
```

- Year is the shipping-bill calendar year, never the document date; unknown parts use
  literal `unknown`. Names come from `rules.batch_name` / `rules.storage_year`.
- Keep original bytes and canonical names. Same name with different content is an
  Error and the original is preserved; no collision suffixes. A corrupt retained PDF is
  restored only from bytes matching the recorded hash.
- Processed JSON holds `metadata`, `content_hash`, `sources`, `layout`,
  `layout_versions`, `fields`, `containers`, `parser_version`. A newer revision
  replaces the obsolete PDF and its JSON/MD.
- Layout is reused only when the PDF hash and layout/rules/mupdf versions match; saved
  business values are inspection output and never reused for extraction.
- Accepted, blank, rejected and runtime-failure outcomes stay distinct; a runtime
  failure is retryable, never a blank.
- Writes are atomic and per-bill locked; same-SB work is never concurrent. Document
  results persist before the workbook update. Directories 0700, files 0600.

## Parsing rules

- Check identity-bearing content on all PDF pages; a contradicting identity rejects the
  document. Preserve page numbers, native block order, text and coordinates; never
  re-sort blocks by library heuristics. Close PDF handles in `finally`.
- Associate identity with the bill header band or the supported secondary Gatepass
  header, not unrelated port/date labels.
- Reconstruct split headings before bounding a cell so CHA text cannot bleed into the
  exporter. Reject address-only exporter and date-only invoice cells.
- Movement tables (flat or stacked) need supported header or continuation geometry;
  unsupported continuations reject instead of truncating.
- Each accepted value carries filename, hash, page, label, quote and bbox. Never guess.
- Fixtures are synthetic; tests assert source association, not just token recognition.

## Workbook safety

- Edits are string-level patches of only the touched `<c>` cells (`ooxml.ts`);
  `styles.xml` gains cloned formats only for date/wrapped cells, all other zip parts
  stay byte-identical. ExcelJS/SheetJS are deliberately not used: they rewrite styles.
- Default is every `FY yyyy-yy` sheet, oldest first, with one workbook save at the end (`--latest`, `--fy` or `--sheet` pick one; `--rows`/`--verify-rows` without a sheet use the newest); unknown sheets and missing or ambiguous
  required columns fail loudly.
- Dates are written as native Excel serials in the sheet's `dd-mmm-yyyy` format.
- Never overwrite formulas or erase existing values on blank, rejected or failed evidence.
- Merged cells resolve to the top-left parent; conflicting updates, unselected rows or
  cancelled rows in the merge block the write.
- One apply and save per run: verify the on-disk hash still matches the opened bytes
  (external-edit guard), write a temp file, copy a `.bak-<stamp>` backup, rename
  atomically, keep the newest five backups.

## CLI

`USAGE` in `src/ts/cli.ts` is authoritative: `-f/--file`, `-s/--sheet`, `--fy`, `--latest`,
`-r/--rows`, `--sb`, `--leo-only`, `-c/--check`, `--verify-rows`, `-n/--dry-run`,
`--force`, `--refresh`, `-j/--workers` (1–8, default 3), `-d/--data-dir`, `-v/--verbose`,
`-q/--quiet`, `--no-color`, `-V`, `-h`. Env: `EXCEL_PATH`, `DAILY_REPORT_DATA_DIR`,
`GWS_BIN`, `NO_COLOR`. Exit status is 1 if any row errored.

## Dashboard

- `server.ts` never imports workflow or workbook code. It runs `src/ts/cli.ts --no-color` with
  arguments built from an allowlist, keeps only the current run's lines in memory, and the page polls
  `/api/log`. A new CLI flag needs no server change unless the dashboard should expose it.
- Binds `127.0.0.1` only; rejects other `Host` headers and any POST that is not same-origin JSON.
  One run at a time (409). Stop sends SIGINT; the workbook is saved only at the very end, so a
  stopped run leaves it untouched.
- The page parses the CLI's tagged lines (`[row N] SB ...`, `[progress] i/N ...`). `[progress]` lines
  are provisional: `workbook.apply` can still downgrade a status, so the row lines printed after
  each sheet are final. Change the line format and `src/ui/index.html` together.

## Privacy and definition of done

- Git-ignored: `data/`, `.env`, `*.pdf`, backups and
  all `*.xlsx` except the root report. Never commit live documents or secrets.
- Done means: `bun test` and `bun check` pass; no dead code, superseded files or unused
  dependencies; `AGENTS.md` and `README.md` match the implemented behavior.
