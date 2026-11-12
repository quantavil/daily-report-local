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
  explicitly configured launch. agent-browser silently relaunches with a temp
  profile if the browser exits, so prefer launching `cloakbrowser-chrome` on the
  personal profile with `--remote-debugging-port=9222` and attaching with
  `agent-browser --cdp 9222`; re-verify `--user-data-dir` before every batch of
  commands, not only at launch. Never open a non-logged-in profile. If the personal profile is unavailable or
  signed out, stop and tell the user; never fall back to another profile.

Creates bills from ICEGATE into SQLite (`data/report.db`) with the `icegate` CLI, using
Gmail as a read-only gatepass fallback, then regenerates `report.xlsx` from the database. SQLite is the product, Excel the residue:
the only hand-typed cells are the three `Remark(...)` columns and any derived column typed over, which are read back into the database before every rewrite. This is the maintenance contract; setup and usage live in `README.md`.

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
- Never derive `ICD stuffing` from a numeric seal (and never add a numeric, channel, container-sharing, LCL or seal-type rule for it). Against the owner's updated Daily Working Report a numeric seal is ICD in 96.4% of bills (2,950 of 3,069) and Factory in the other 3.6% (119). Why: a container stuffed at the factory carries the factory's alphanumeric seal (BOLT, KLIK, ENOS, RFID/ESEAL); when that seal is broken at the ICD (examination, or restuffing) customs attaches a numeric bottle seal (`BTSL`), so a numeric seal means ICD *or* a factory stuffing whose seal was broken, and the broken seal is a physical event no field in our data records. Tested and rejected, each worse than the numeric-blank baseline or failing a train-old/test-new split: RED or green channel, containers shared with a RED bill (96.0% down to 92.6%), seal series, sibling containers, port, size, exporter, weight, FOB, dates, gatepass seal type (`BTSL` in 116 of 119 Factory and in 2,910 ICD bills). Numeric-seal bills stay blank and are set only from the owner's labelled workbook with `stuffing-set`; the one-off fill of 3,283 numeric-seal bills (3,168 ICD, 115 Factory) came from the owner's Daily Working Report workbook, which is not kept in this project.
- Parse the gatepass by label-anchored layout. Regex may recognize labels and validate
  tokens; it must not pick arbitrary nearby names, dates or numbers.

| Module | Responsibility and boundary |
| --- | --- |
| `cli.ts` | Argument parsing, output, exit status; subcommands `iec-missing`, `iec-set`, `mark-manual`, `stuffing-set`, `fields` |
| `workflow.ts` | Discovery, per-bill fetch, merge into the database, report plan; no labels or patterns |
| `rules.ts` | All ICEGATE policy; no network, filesystem or Excel side effects |
| `models.ts` | Layout types |
| `icegate/client.ts` | Spawns `icegate ... --json`; the only code that talks to ICEGATE; never sees credentials |
| `icegate/schemas.ts` | valibot shapes of the JSON and the field catalog (database columns, export picker) |
| `store/db.ts` | SQLite schema, merge and current-status refresh, IEC-name map, gaps, status; no ICEGATE or Excel code |
| `gmail.ts` | Read-only Gmail search through `gws`: gatepass PDF retrieval, and per-bill evidence (attachment file names, To addresses of the owner's own mail with attachments); nothing else is downloaded |
| `gatepass/parse.ts` | Native layout via mupdf and rule-driven parsing of the SB35 gatepass and the SB e-copy; no network, no Excel |
| `excel/export-map.ts` | Loads and validates `export-map.json` |
| `excel/read.ts` | Reads cell text back from a workbook (ours or one saved by Excel/LibreOffice); no database, no rules |
| `excel/report.ts` | Writes `report.xlsx` from scratch on fflate (atomic rename), and `report.csv` beside it from the same sheet rows (all sheets in order, same headers and cells, dates as ISO text; never read back), including the `highlight` Excel rule (green target cell where the flag column is Y) and the Y/N dropdown; no business rules |
| `server.ts` | `bun run dev` localhost dashboard (`src/ui/index.html`): spawns the CLI, reads `report.db` read-only, saves `export-map.json`; imports no fetch or workflow code |

## Data sources

- Bills are created from two ICEGATE lists: the Job Status report (also the IEC per bill) and
  the SB35 eGatepass search, queried one month at a time from `--from` to today. Neither needs
  a workbook row. Discovery sees only what these lists show for the logged-in ICEGATE ID: the Job Status report holds the portal's latest ~200 rows and takes no date range; a bill missing from both is not found (a bill filed under another ICEGATE ID is in neither; smaller eGatepass windows return the same set). A gatepass record whose SB date is not shortly before its job date (the portal lists placeholders such as 20-12-2002, mostly in 2024, and gives such a record no IRN to download) takes its identity from the PDF instead: Gmail is searched by SB number and the job date's financial year, and a PDF whose headers name that SB number and port, with a plausible SB date, creates the bill under the PDF's date and is kept; without such a PDF the record is skipped, and an existing bill with that SB number is not searched again. The portal's month window filters on the job date. Public SB enquiry: LEO date, invoices, job number, EGM containers.
  SB35 eGatepass (`ecopy search GATEPASS` for the SB date alone, since gatepass records carry the SB date, matched on `docNumber` = SB number. A batch run asks `plan()` first so a month with several SB dates is one search;
  its job number is a per-day counter, unrelated to the enquiry job number): container,
  size, seal, the IEC (`IEC/Br`) and the `3.Exporter` name. The gatepass is the main PDF read and also gives the `14.Facilitation Category`. The SB e-copy (`ecopy search SB`, a snapshot at filing, available before LEO) is read only as the last resort for IEC and exporter, after the gatepass step left them blank; only those two values are stored and the PDF is not kept. The SB e-copy search row also carries `jobNumber`, the daily job counter (e.g. 953), stored as `sb_job_no` for every bill the SB step visits, with no download needed; it is a different number from the enquiry `Job No` (`gatepass/parse.ts` `parse_sbcopy`). If ICEGATE cannot supply a valid PDF, Gmail searches the bill financial year then later mail; all candidate PDFs must pass the same identity validation. Each parsed gatepass is kept as `data/gatepass/<sb_date>_<sb_no>.pdf` (git-ignored, mode 0600); a rejected one is not. A kept PDF is parsed again instead of searching and downloading. Portal placeholders (`N.A.`, `-`) are blank.
- Exporter name (database column and report column `Exporter`), first non-empty wins: the IEC-name map (`iec_names`: My IEC or user-approved
  via `iec-set`), then a name the gatepasses already gave that same IEC on other bills (used only when all of them agree; two spellings stay blank until `iec-set`), then this bill's own gatepass exporter, then the SB e-copy exporter. IEC maps to exporter name, so each name is learned once and reused. Note from the owner: My IEC lists importer IECs, not export clients, so it fills few exporter names; most come from gatepasses and `iec-set`. A bill with no IEC cannot be named from the map and waits for its gatepass. The
  enquiry IEC is masked and never used. The IEC itself is Job Status first, else the gatepass `IEC/Br`;
  a gap in it triggers a gatepass fetch like the other gatepass fields.
- Merge rule: first non-empty value wins except current portal status, which is updated by a later non-empty enquiry response. Dates are stored
  as ISO. Containers, sizes and seals are ordered records keyed by container number; a gatepass container and an EGM container with the same seal are one box and keep the EGM number (`same_box_by_seal`).
- Deliberately not stored (no report column, nothing reads them): the enquiry's e-document
  list, portal error-text fields (job, gateway, EGM), the gatepass IRN and PDF hash (the
  `fetched_gatepass` stamp already records the parse), and the bill-level `egm_filed` and
  gateway EGM no/date (duplicates of the per-container `egm_no`/`egm_date`). Do not re-add
  them without a consumer. Job No and Job Date are kept but do not gate `Success`; the report column `SB Job No` (in the old Job No position) exports `sb_job_no` (the SB e-copy daily counter), while the Job Status / enquiry `job_no` stays in the database only.
- Per-run gatepass counts distinguish ICEGATE, Gmail, saved PDFs with unknown origin, unavailable documents and errors. A bill is complete when `store/db.ts` `gaps()` is empty; its status is then `Success`.

## Identity

- The database key is financial year + SB number (`fy` from the SB date), never duplicated;
  a second date or port for the same key is an Error on that bill, never merged. Identity is
  built by `rules.ts` `bill_identity`; an invalid row from a portal list is skipped and blocks
  no other bill. Cancellation is derived by `rules.ts` `is_cancelled` from portal cancellation statuses (`CANCELLED_STATUS`, including `Purge after 30 days`, `SB is Cancelled`, `LEO CANCELLED`, `CANCELLED (set by hand)`), and stale examination queues (`TO BE MARKED` / `TO BE EXAMINED` older than 30 days without LEO),
  never inferred from anything else.

## Run semantics

- Normal run: discover (Job Status and My IEC once, a failure there stops the run before any
  change; then one gatepass query per month, a failed month is a warning), apply IEC names,
  then fetch for every bill that is not `Success`, `Cancelled` or `Manual` (never queried again unless `--force`). The enquiry runs when LEO
  date, invoice or container is missing; the gatepass when container, size, seal, IEC or exporter
  is missing. ICEGATE is queried when a LEO date is known; Gmail fallback can also run without a LEO date. A bill the portal
  answers nothing for is not stamped as fetched, so it stays `Not found` on every run.
  Failures are reported on the bill and never block others.
- Statuses: `Success`, `Partial`, `Cancelled`, `Manual` (closed by hand with `mark-manual`, the user's decision that a field cannot be obtained; never queried again unless `--force`), `Not found` (not known to the portal),
  `Error` (runtime or extraction failure, run output only).
- Export is last and complete: every bill in the database is written, one `FY yyyy-yy` sheet
  per year oldest first, rows by SB date. `Type of Stuffing (derived)` comes from `rules.ts` `stuffing_of` (the only rule: letters in every seal mean factory stuffing, the owner's updated workbook agrees for 98% of those bills; numeric, mixed or missing seals stay blank because a numeric seal is mostly ICD but can be Factory when the seal was broken at the ICD, and no field in our data tells them apart; a value typed by the owner (the `stuffing-set` command, or typed over the cell in Excel) is the `stuffing` note, wins over the rule, and numeric-seal bills are filled that way; otherwise computed, never stored). `Type Shipment (derived)` is `LCL` only when a
  container is shared by bills of different non-empty exporters dated within `LCL_WINDOW_DAYS`; it is computed, never stored unless typed over. Derived and hand-typed columns get an amber header (users and agents may overwrite them and the typed value lasts; clearing the cell brings the computed value back). A typed `Status` changes only the report, never which bills are fetched.
- Typed-in columns (`notes.*` ids: `Remark(Agent email)`, `Remark(Bill no.)`, `Remark(Own job Y/N)`; and the `derived.*` ids `Status`, `Type of Stuffing (derived)`, `Type Shipment (derived)`) live in table `notes`, kept across rebuilds; a derived note wins over the computed value. Before each report write `workflow.ts` `keep_notes` reads the workbook back (`excel/read.ts`) and stores every cell that differs from `notes_written`, every typed-in column as the workbook showed it when this program last wrote it (`publish_report`, blanks included); a cleared cell clears the note, and an out-of-date workbook changes nothing because its cells equal what was written. A derived cell with no snapshot (workbook from before this existed) is skipped once, since it cannot be told from a typed one. Bills are matched by SB number and SB date, so the map needs both columns. An unreadable workbook stops the run and is not overwritten.
- Gmail own-job and agent email (`workflow.ts` `read_mail`, rules in `rules.ts` `job_tokens`, `own_job_of`, `agent_address`): after the ICEGATE fetch, once per bill dated from the run's `--from` with a LEO date that is not cancelled (table `mail_checked`). The attachment file name of the owner's mail carries the checklist token `EXP-yyyy-n` / `VIDE-EXP-yyyy-n`: one token is `Y`, several write `CHECK: …` into the own-job note for the owner to settle, none with mail present is `N`. The agent email is the non-owner, non-ICEGATE, non-excluded (`NOT_AGENT_ADDRESS`, currently bhopalmehta83@ and deependrasingh215@gmail.com, not exporters) To address that appears most often; tied addresses are all written, comma-separated, for the owner to trim; when To holds no outside address Cc is used; none stays blank. Results are inserted into `notes` only where no note exists (typed values win; a cleared cell is not refilled). Never download or read the checklist PDFs; there is no agent-code column. A Gmail failure stops the step with one warning and leaves bills unread. Measured on 50 FY 2026-27 bills against the owner's notes: own job agreed on 46, 3 listed several tokens, none contradicted; agent email was found for 17 and always matched the agent code.
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

- `report.xlsx` is generated, never patched: a temp file is renamed over it atomically, and the file it replaces is first copied to `report.xlsx.bak` (git-ignored), the one backup kept.
  Only the typed-in columns (`Remark` and derived, see above) are read back (`keep_notes`); every other hand edit is lost by design.
- Schema changes bump `SCHEMA_VERSION` in `store/db.ts`; fetched bill tables are rebuilt and
  refetched, the IEC-name map is kept, and the old file is copied to `report.db.bak-v<N>` first.
- A portal date that is not a date is a `DataError` on that bill (nothing stored); only `DataError` is skipped during discovery, any other failure stops the run.

## Security and privacy

- All ICEGATE calls are read-only; live uploads, staging or submits need explicit user
  authorization every time. Never print, log or commit credentials, OTPs, tokens, session
  ids, cookies or handoff URLs. TLS verification stays on; no automatic POST retries.
- Committed on purpose, by the owner's decision, to a public repo: `data/report.db`, `report.xlsx`
  (added with `git add -f`; other `*.xlsx` stay ignored) and `data/missing-from-login.txt`. They hold client data.
  Git-ignored: `data/gatepass/`, `.env`, `*.pdf`, backups and every other `*.xlsx`. Never commit PDFs,
  live documents or secrets.
- Git history is trimmed by `bun run purge-history [--days 30] [--apply]` (`scripts/purge-history.ts`): commits older than 30 days become one baseline commit, then a force-push with lease; dry run without `--apply`, no backup kept. It refuses a dirty tree or a branch that differs from origin, verifies the squashed tree equals the old one before pushing, and restores the old history if a check or the push fails (`tests-ts/purge-history.test.ts`). Old clones and forks keep the old data.
- Done means: `bun test` and `bun check` pass; no dead code, superseded files or unused
  dependencies; `AGENTS.md` and `README.md` match the implemented behavior.
