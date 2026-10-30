# AGENTS.md — daily-report-local

## Browser profile guard

- For browser research, use only the user's personal Cloak profile at
  `/home/quantavil/Documents/ALWAYS-USE-PROFILE` with
  `/home/quantavil/.local/bin/cloakbrowser-chrome`. Never start or use an
  ephemeral or default browser profile.
- Start the named `icegate-profile` session with both `--profile` and
  `--executable-path` explicitly set on the first command. Before using the
  session, verify the browser process has the personal `--user-data-dir`.
  If it does not, stop, close that temporary session, and relaunch correctly.
  Do not run a tab-list or other command that can start a session before the
  explicitly configured launch. If the personal profile is unavailable or
  signed out, stop and tell the user; never fall back to another profile.

Creates bills from ICEGATE into SQLite (`data/report.db`) with the `icegate` CLI, using
Gmail as a read-only gatepass fallback, then regenerates `report.xlsx` from the database. SQLite is the product, Excel the residue:
nobody fills anything by hand. This is the maintenance contract; setup and usage live in `README.md`.

## Toolchain
- Bun only: `bun install`, `bun run sync ...`, `bun run dev`, `bun test`, `bun check`.
- Code in `src/ts/`, tests in `tests-ts/`. Split modules only for demonstrated need.
- No AI, no OCR, no browser in the application: unreadable or unsupported PDFs yield explicit rejected/error outcomes. Gmail is a read-only gatepass fallback via `gws`.
- `mupdf` is AGPL-3.0: fine for private local use; switch to `pdfjs-dist` if distributed.

## Design discipline

- One implementation per responsibility. Delete replaced code, dead helpers, unused
  dependencies, obsolete tests and stale docs. No compatibility shims.
- All executable ICEGATE policy (labels, geometry tolerances, token validation, identity and
  date parsing, status patterns) lives in `src/ts/rules.ts`. Other modules and this file
  must not restate it. Which field feeds which column is `export-map.json`, validated
  against the catalog in `icegate/schemas.ts`.
- Parse the gatepass by label-anchored layout. Regex may recognize labels and validate
  tokens; it must not pick arbitrary nearby names, dates or numbers.

| Module | Responsibility and boundary |
| --- | --- |
| `cli.ts` | Argument parsing, output, exit status; subcommands `iec-missing`, `iec-set`, `fields` |
| `workflow.ts` | Discovery, per-bill fetch, merge into the database, report plan; no labels or patterns |
| `rules.ts` | All ICEGATE policy; no network, filesystem or Excel side effects |
| `models.ts` | Layout types |
| `icegate/client.ts` | Spawns `icegate ... --json`; the only code that talks to ICEGATE; never sees credentials |
| `icegate/schemas.ts` | valibot shapes of the JSON and the field catalog (database columns, export picker) |
| `store/db.ts` | SQLite schema, merge and current-status refresh, IEC-name map, gaps, status; no ICEGATE or Excel code |
| `gmail.ts` | Read-only Gmail search and PDF attachment retrieval through `gws` |
| `gatepass/parse.ts` | Native layout via mupdf and rule-driven parsing of the SB35 gatepass; no network, no Excel |
| `excel/export-map.ts` | Loads and validates `export-map.json` |
| `excel/report.ts` | Writes `report.xlsx` from scratch on fflate (atomic rename); no business rules |
| `server.ts` | `bun run dev` localhost dashboard (`src/ui/index.html`): spawns the CLI, reads `report.db` read-only, saves `export-map.json`; imports no fetch or workflow code |

## Data sources

- Bills are created from two ICEGATE lists: the Job Status report (also the IEC per bill) and
  the SB35 eGatepass search, queried one month at a time from `--from` to today. Neither needs
  a workbook row. Public SB enquiry: LEO date, invoices, job number, EGM containers.
  SB35 eGatepass (`ecopy search GATEPASS` for the SB date alone, since gatepass records carry the SB date, matched on `docNumber` = SB number. A batch run asks `plan()` first so a month with several SB dates is one search;
  its job number is a per-day counter, unrelated to the enquiry job number): container,
  size, seal, the IEC (`IEC/Br`) and the `3.Exporter` name. The gatepass is the only PDF read; there is no SB
  e-copy. If ICEGATE cannot supply a valid PDF, Gmail searches the bill financial year then later mail; all candidate PDFs must pass the same identity validation. Each parsed gatepass is kept as `data/gatepass/<sb_date>_<sb_no>.pdf` (git-ignored, mode 0600); a rejected one is not. A kept PDF is parsed again instead of searching and downloading. Portal placeholders (`N.A.`, `-`) are blank.
- Exporter name (database column and report column `Exporter`), first non-empty wins: the IEC-name map (`iec_names`: My IEC or user-approved
  via `iec-set`), then the gatepass exporter. This is the one human-approved lookup. The
  enquiry IEC is masked and never used. The IEC itself is Job Status first, else the gatepass `IEC/Br`;
  a gap in it triggers a gatepass fetch like the other gatepass fields.
- Merge rule: first non-empty value wins except current portal status, which is updated by a later non-empty enquiry response. Dates are stored
  as ISO. Containers, sizes and seals are ordered records keyed by container number.
- Deliberately not stored (no report column, nothing reads them): the enquiry's e-document
  list, portal error-text fields (job, gateway, EGM), the gatepass IRN and PDF hash (the
  `fetched_gatepass` stamp already records the parse), and the bill-level `egm_filed` and
  gateway EGM no/date (duplicates of the per-container `egm_no`/`egm_date`). Do not re-add
  them without a consumer. Job No and Job Date are kept and exported but do not gate `Success`.
- Per-run gatepass counts distinguish ICEGATE, Gmail, saved PDFs with unknown origin, unavailable documents and errors. A bill is complete when `store/db.ts` `gaps()` is empty; its status is then `Success`.

## Identity

- The database key is financial year + SB number (`fy` from the SB date), never duplicated;
  a second date or port for the same key is an Error on that bill, never merged. Identity is
  built by `rules.ts` `bill_identity`; an invalid row from a portal list is skipped and blocks
  no other bill. Cancellation is derived from the portal's current status (`CANCELLED_STATUS`), including `Purge after 30 days`,
  never inferred from anything else.

## Run semantics

- Normal run: discover (Job Status and My IEC once, a failure there stops the run before any
  change; then one gatepass query per month, a failed month is a warning), apply IEC names,
  then fetch for every bill that is not `Success` or `Cancelled` (never queried again unless `--force`). The enquiry runs when LEO
  date, invoice or container is missing; the gatepass when container, size, seal, IEC or exporter
  is missing. ICEGATE is queried when a LEO date is known; Gmail fallback can also run without a LEO date. A bill the portal
  answers nothing for is not stamped as fetched, so it stays `Not found` on every run.
  Failures are reported on the bill and never block others.
- Statuses: `Success`, `Partial`, `Cancelled`, `Not found` (not known to the portal),
  `Error` (runtime or extraction failure, run output only).
- Export is last and complete: every bill in the database is written, one `FY yyyy-yy` sheet
  per year oldest first, rows by SB date. `Type Shipment` is `LCL (derived)` only when a
  container is shared by bills of different non-empty exporters; it is computed, never stored.
- `--check` is read-only (no network, no report write). `-n` does everything except write the
  report. `--excel` only rewrites the report from the database (no ICEGATE calls; not combinable with `--from`, `--to` or `--force`). `--from` sets the first discovery month. A
  gatepass the portal is still preparing leaves its fields blank for a later run.
- Exit status is 1 if any bill errored.

## Parsing rules (gatepass)

- Check identity-bearing content on all PDF pages; a contradicting identity rejects the
  document. Preserve page numbers, native block order, text and coordinates. Close PDF
  handles in `finally`. Several different exporters reject the document.
- Movement tables (flat or stacked) need supported header or continuation geometry;
  unsupported continuations reject instead of truncating. Invalid tokens reject the whole
  document; never guess.
- Fixtures are synthetic; tests assert source association, not just token recognition.

## Report safety

- `report.xlsx` is generated, never patched: a temp file is renamed over it atomically.
  Nothing reads it back, so hand edits are lost by design.
- Schema changes bump `SCHEMA_VERSION` in `store/db.ts`; fetched bill tables are rebuilt and
  refetched, the IEC-name map is kept, and the old file is copied to `report.db.bak-v<N>` first.
- A portal date that is not a date is a `DataError` on that bill (nothing stored); only `DataError` is skipped during discovery, any other failure stops the run.

## Security and privacy

- All ICEGATE calls are read-only; live uploads, staging or submits need explicit user
  authorization every time. Never print, log or commit credentials, OTPs, tokens, session
  ids, cookies or handoff URLs. TLS verification stays on; no automatic POST retries.
- Git-ignored: `data/`, `.env`, `*.pdf`, backups and all `*.xlsx`.
  `report.db` holds client data (mode 0600). Never commit live documents or secrets.
- Done means: `bun test` and `bun check` pass; no dead code, superseded files or unused
  dependencies; `AGENTS.md` and `README.md` match the implemented behavior.
