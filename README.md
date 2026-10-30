# Daily Report Sync

Fills every financial-year sheet (`FY 2023-24` … `FY 2026-27`) of `Daily Working Report.xlsx`
from the ICEGATE PDFs in Gmail. Rows already marked `Success` are skipped, so finished
years cost nothing; only new or unfinished rows are looked up. No AI, no OCR.

## Setup

Needs [Bun](https://bun.sh) and the Google Workspace CLI `gws`, logged in read-only
(`gws auth login`).

```bash
bun install
```

## Use

```bash
bun run sync                   # fill all FY sheets (new / unfinished rows only)
bun run sync -c                # only list rows with missing fields (no Gmail, no writes)
bun run sync -n                # dry run: workbook is not saved; PDF artifacts are retained
bun run sync --sb 7546404      # one shipping bill
bun run sync -r 1349-1351      # rows 1349 to 1351 (of the latest sheet; add --fy for another)
bun run sync --latest          # only the newest FY sheet
bun run sync --fy 2025-26      # only that year's sheet
bun run sync --force           # also recheck Success rows for revised PDFs
```

Prefer buttons? `bun run dev` opens a local dashboard at http://127.0.0.1:4317: pick the
sheets, press Sync, Check, Dry run or Verify rows, and watch progress and per-bill status live.
It runs the same CLI underneath, only listens on this computer, and does one run at a time.

Output:

```text
[workbook] Daily Working Report.xlsx
[sheets] all: FY 2023-24 · FY 2024-25 · FY 2025-26 · FY 2026-27
[sync] 4 to process · 1832 skipped (Success, cancelled or invalid; --force rechecks Success)
[2026-27 row 1293] SB 7546404  Partial    LEO date found; 3 fields still missing
[done] Success 3 · Partial 1 · 4.2s · workbook saved
```

During processing the CLI shows a live counter in a terminal, or `[progress]` lines
when output is piped. These updates are provisional; the row statuses printed after
each sheet reflect workbook safety checks. `--quiet` suppresses progress.

`bun run sync -h` lists every option (other sheet, other workbook, LEO-only, read-only
verify against saved PDFs, worker count, data folder). Defaults come from `.env`
(`EXCEL_PATH`, `DAILY_REPORT_DATA_DIR`).

## What it fills

| PDF | Fills |
| --- | --- |
| SB21 First Copy | Exporter name, invoice number |
| SB22 LEO Copy | LEO date |
| SB35 eGatepass | Container, size, seal |

A row becomes `Success` only when all six fields are verified against the PDFs; after that
it is never touched again unless you pass `--force`. Anything unclear is left blank or
unchanged and the row stays `Partial`. Cancelled bills (Billing Status `CANCELLED`) are
ignored. Nonempty invalid bill identities are reported as row errors, and `--check`
returns a nonzero exit status when it finds one.

## Safety

- Gmail is read-only.
- Only the cells it fills are changed in the workbook; styles, merges and formulas are
  untouched. A timestamped backup is made before every save (newest five kept), and the
  save aborts if the file was edited meanwhile.
- PDFs and per-bill results are kept under `data/` (git-ignored).

## Development

```bash
bun test
bun check
```

Code is in `src/ts/`, tests in `tests-ts/`; rules and module boundaries are in
[AGENTS.md](AGENTS.md).
