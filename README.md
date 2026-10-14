# Daily Report Sync

Local Python CLI that fills `Daily Working Report.xlsx` from PDF attachments sent
by `noreply@icegate.gov.in`. It uses workbook identity, canonical filename metadata,
native PDF geometry, and deterministic rules. There is no AI extraction or fallback.

## Setup

Requires Python 3.14+ and uv.

```bash
uv sync
cp .env.example .env
```

Enable Gmail API and place OAuth Desktop-client credentials in `credentials.json`.
The first extraction run opens Gmail consent and creates `token.json`; access is
read-only. No AI API key is needed. Paths can be configured in `.env`.

## Authoritative sources

| Document | Extracted fields | Original pages |
| --- | --- | --- |
| SB21 First Copy | Exporter from `1. EXPORTER'S NAME & ADDRESS`; primary invoice from `2. INVOICE No. & Dt.` | 1–2 |
| SB22 LEO Copy | LEO from `6. LEO Date` | 1 |
| SB35 eGatepass | Container/size/seal movement records (`11. Container`, `14. Size`, `12. Seal`) | Relevant pages and continuations |

The expected sequence is SB21 → SB22 → SB35. Available documents can fill their
owned fields independently. No field borrows a value from another document type.
LEO is a native Excel date, never the filename document date. Exporter names exclude
addresses and CHA companies; invoice values exclude dates, amounts, and currency.

## Workflow and private records

1. Select Excel rows and read SB number, SB date, port, and any explicit document date.
2. Skip Gmail at SB date + 30 days only when every selected row for the bill has
   Status `Success` and fresh local parsing verifies a complete, issue-free batch. Otherwise search fresh sender-approved metadata by SB
   number and `No<SB>Dt<DDMMYYYY>`, then match filenames locally. Unknown SB dates
   continue discovery; cancelled rows always skip it.
3. Select the newest filename timestamp per document type and retain only those
   copies: at most three PDFs per batch. Equal timestamps with different bytes are
   rejected. Save a newer copy safely before deleting its predecessor and matching
   processed JSON/MD; failed downloads preserve local originals and block affected
   fields during live checking.
4. Verify PDF identity and parse label/cell relationships from native text coordinates.
   Reuse unchanged native layouts and parse them once each run; saved field values
   are inspection output, never an extraction shortcut.
5. Persist source-bound results, then apply permitted workbook updates sequentially.
   Save a timestamped backup before atomic replacement; retain the newest five.

```text
data/2026/754640407102026INMBD6/
├── pdfs/
│   └── <canonical filename>.pdf
├── processed/
│   ├── <same filename stem>.json
│   └── <same filename stem>.md
└── result.json
```

The parent year is the shipping-bill calendar year, derived from filename SB date;
a December bill with a January LEO stays in the December bill’s year. Unknown SB
dates use an `unknown` year. The batch name is SB number + SB date + port.
Each document JSON contains filename
metadata, content hash, attachment sources, native layout and its versions, field
outcomes, ordered containers, and parser version. `result.json` records the combined
result, selected sources/hashes, and workbook-provided identity separately. Unknown
identity components use literal `unknown` in pending folders; matching filenames
resolve document batches without merging conflicting identities.

Canonical filenames are expected to be unique. Unexpected different bytes for the
same name report Error and preserve the original. Corrupt retained PDFs can be
restored only from a download matching their recorded hash when discovery is needed.

Unchanged PDFs need no repeated download. Reuse uses stable message/MIME-part
identity rather than Gmail attachment tokens, which can rotate between reads. Rules,
layout profile, and PyMuPDF versions govern layout reuse. `--refresh` re-extracts
native layout from stored PDFs; verification parses reusable layout afresh without
workbook writes. Corrupt processing artifacts are regenerated. Markdown comes from
the same native cells, with no second converter. Containers stay ordered, including
blank positions, until Excel serialization. Persistence, API, and extraction failures
are reported rather than cached as blank fields.

At or after SB date + 30 days, Gmail is skipped only when all selected rows for the
bill have Status `Success` and fresh native parsing of all three retained PDFs verifies every required owned
field, complete aligned container records, and no issues/runtime failures. Excel
Success alone or previously AI-filled values never establish this. Partial, Not found,
and Error rows keep checking Gmail regardless of age, even with complete local proof;
successful batches younger than 30 days also check revisions. `--force` and
`--refresh` preserve this policy. Local parsing always uses verified native layout,
never saved field values. Missing or damaged local PDFs trigger discovery.

`result.json` preserves newest-observed candidates across scoped runs, including
failed downloads; selected hashes describe only validated retained bytes. A known
newer failure blocks fallback to an older PDF. Failures stay isolated by document
type, so damaged SB21 does not block valid SB22/SB35.

OCR is disabled. Unsupported layouts, missing labels, ambiguous cells, conflicting
identities, and unreadable PDFs leave affected existing values intact. The parser
supports tested native label/cell, flat movement-table, and stacked movement-record
layouts; a new layout needs a rule and an anonymized regression fixture.

## Commands

Default sheet is `FY 2026-27`; unknown sheets fail explicitly.

```bash
# Read-only missing-field audit; no Gmail access
uv run --no-sync sync.py --check

# Fill missing values
uv run --no-sync sync.py
uv run --no-sync sync.py --sb 7546404
uv run --no-sync sync.py --rows 1349-1351 --sheet "FY 2026-27"

# Read-only source comparison with fresh local parsing and evidence
uv run --no-sync sync.py --verify-rows 2-20 --leo-only

# Update filled LEO cells only from validated SB22 evidence
uv run --no-sync sync.py --sb 7546404 --force --leo-only

# Re-extract native layout from local PDFs; downloads are still reusable
uv run --no-sync sync.py --sb 7546404 --force --refresh

# Choose workbook, private records directory, and concurrency
uv run --no-sync sync.py --file report.xlsx --data-dir /private/icegate --workers 3
```

`--force` permits overwriting selected filled fields but never erases values on
missing/rejected evidence or overwrites formulas. `--leo-only` restricts data writes
to LEO and discovery to SB22. Full sync retains only the newest copy of each document type for
selected bills. Normal sync also checks filled rows whose sources are incomplete or
unverified; filled values remain intact unless `--force` permits an update. Mismatched
filled values or missing/rejected selected fields prevent Success.
Retained container columns must match the selected source before filling another
member of their triplet. Row/SB filters apply before row validation and to status
writes too; merged cells crossing unselected or cancelled rows are blocked. A blocked
container member blocks the whole proposed triplet; conflicting shared-parent
proposals never use completion order to choose.
Billing Status `CANCELLED` rows are skipped; Status `Cancelled` alone does not cancel a row. Fetch/parse workers are limited to eight; workbook writes
are sequential. The former AI cache controls and broad/historical source fallback are
removed; `--no-cache` is not an option.

Status is `Success`, `Partial`, `Cancelled`, `Not found`, or `Error`. Success means
required-field completeness with no unresolved selected-field validation; Partial
includes pending/rejected fields or incomplete container positions. Error means a
runtime/API/extraction failure. Audit output includes field-specific reasons/evidence.

## Development and privacy

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check src/ tests/ sync.py
```

The package is `src/daily_report/`; `sync.py` is its launcher. Rules and module
boundaries are documented in [AGENTS.md](AGENTS.md). Tests use synthetic workbooks,
PDFs and layouts, with mocked Gmail boundaries.

Private working records, caches, PDFs, `.env`, credentials, tokens, backups, and other
workbooks are git-ignored. Only the root [Daily Working Report.xlsx](Daily%20Working%20Report.xlsx)
is explicitly approved for tracking and public publication. This remains a committed
snapshot; local workbook changes are not published automatically.
