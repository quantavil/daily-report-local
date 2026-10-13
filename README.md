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
2. Build the longest known filename prefixes. Search fresh Gmail metadata by the
   SB number and generated `No<SB>Dt<DDMMYYYY>` subject token within the approved
   sender, then match prefixes and full identity locally. Gmail filename queries do not reliably match partial canonical names.
3. Retain matching revisions and select the newest document timestamp encoded in
   the filename. Equal timestamps with different bytes are rejected.
4. Verify PDF identity and parse label/cell relationships from native text coordinates.
   Reuse unchanged native layouts and version-matching parsed artifacts. Revalidate cached business
   values against native layout before accepting them.
5. Persist source-bound results, then apply permitted workbook updates sequentially.
   Save a timestamped backup before atomic replacement; retain the newest five.

```text
data/7546404/07102026-INMBD6/
├── manifest.json
├── pdfs/                      # Originals; name collisions retain both versions
├── processed/<PDF hash>/
│   ├── layout.json
│   ├── document.md
│   └── parsed.json
└── result.json
```

Date/port subfolders prevent unrelated bills with the same SB number from sharing
results. Unknown components have explicit `unknown` folder segments; multiple matching
identities require resolving the workbook identity. Filenames, hashes, original pages,
labels, quotations, and coordinates retain provenance. Containers remain aligned
records, including blank positions, until Excel serialization.

Unchanged PDFs need no repeated download. Reuse uses stable message/MIME-part
identity rather than Gmail attachment tokens, which can rotate between reads. Rules, parser, layout profile, and converter
versions govern reuse. `--refresh` reparses stored PDFs; read-only verification always
uses fresh local parsing. Corrupt processing artifacts are regenerated. Persistence,
API, and conversion failures are reported rather than cached as blank fields.

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

# Reparse local PDFs; downloads are still reusable
uv run --no-sync sync.py --sb 7546404 --force --refresh

# Choose workbook, private records directory, and concurrency
uv run --no-sync sync.py --file report.xlsx --data-dir /private/icegate --workers 3
```

`--force` permits overwriting selected filled fields but never erases values on
missing/rejected evidence or overwrites formulas. `--leo-only` restricts data writes
to LEO and discovery to SB22. Full sync retains all matching document types for
selected bills. Completed rows require force or verification to be reconsidered.
Retained container columns must match the selected source before filling another
member of their triplet. Row/SB filters apply before row validation and to status writes too; merged cells crossing unselected rows are
blocked, and conflicting shared-parent proposals never use completion order to choose.
Cancelled rows are skipped. Fetch/parse workers are limited to eight; workbook writes
are sequential. The former AI cache controls and broad/historical source fallback are
removed; `--no-cache` is not an option.

Status is `Success`, `Partial`, `Cancelled`, `Not found`, or `Error`. Success means
required-field completeness with no unresolved selected-field validation; Partial
includes pending/rejected fields or incomplete container positions. Error means a
runtime/API/conversion failure. Audit output includes field-specific reasons/evidence.

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
