# Daily Report Sync

Creates every shipping bill from ICEGATE into a local SQLite database (`data/report.db`),
fetches what each bill lacks, then rewrites `report.xlsx` from the database: one sheet per
financial year (`FY 2023-24` … `FY 2026-27`), oldest bill first. The database is the product;
the report is regenerated from it every run. No Gmail, no browser, no AI, no OCR, no manual
data entry.

## Setup

Needs [Bun](https://bun.sh) and the `icegate` CLI from `~/Documents/icegate-client`
(logged in through its own `.env`; override with `ICEGATE_BIN` / `ICEGATE_DIR`).

```bash
bun install
```

## Use

```bash
bun run sync                    # create bills, fetch what they lack, rewrite report.xlsx
bun run sync -c                 # list bills with missing fields, then a count per missing field (no network)
bun run sync -n                 # dry run: the database is updated, report.xlsx is not written
bun run sync --excel            # only rewrite report.xlsx from what report.db holds (no ICEGATE calls)
bun run sync --from 2025-04-01  # discover bills from that month on
bun run sync iec-missing        # IECs that still need an exporter name
bun run sync iec-set IEC "NAME" # approve the exporter name for an IEC
bun run sync fields             # fields that can be exported
```

Without `--from`, discovery starts at the month before the newest stored bill (the first run
starts at `2023-03-01`, the portal's first month), so a daily run is quick.

`bun run dev` opens a local dashboard at http://127.0.0.1:4317: the database view (bills,
gaps, IECs waiting for a name), a picker for which database field goes into which report
column, and Sync / Preview / Check / Write report. It runs the same CLI, only listens on this
computer, and does one run at a time.

`bun run sync -h` lists every option. Defaults come from `.env` (`EXCEL_PATH`,
`DAILY_REPORT_DATA_DIR`).

## How bills appear

1. The Job Status report and the SB35 eGatepass search (one query per month) list the bills.
   Both create bills, so no workbook row or email is needed.
2. The public SB enquiry fills LEO date, job, invoices and EGM containers.
3. The eGatepass PDF (the only document read) fills container, size, seal, the IEC (`IEC/Br`) and the exporter. Each parsed PDF is kept in `data/gatepass/` (git-ignored) and re-parsed from there, never downloaded twice.

## The report

Columns are `export-map.json` (default below; edit in the dashboard or by hand). The file is
rebuilt from the database each run, so never edit it by hand.

| Column | Source |
| --- | --- |
| Shipping Bill Date / No, Port Code / ICD Code | bill identity |
| LEO Date | public SB enquiry |
| Job No / Job Date | Job Status report, then the enquiry |
| Exporter | IEC name (My IEC or `iec-set`), else the gatepass `3.Exporter` |
| IEC | Job Status report, else the gatepass `IEC/Br` |
| Inv No / Inv Date | public SB enquiry |
| Container No. / Size / Customs Seal Number | EGM list in the enquiry, then the gatepass |
| EGM No / EGM Date | public SB enquiry, per container |
| Type Shipment | `LCL (derived)` when a container is shared by bills of different exporters, else blank |
| Current Status | the portal's own status text |
| Status | `Success`, `Partial`, `Cancelled` or `Not found` |

The sheets follow the old daily report: dark-blue header, alternating white and pale-grey rows, thin borders, frozen header, filter, green tabs, widths fitted to the content.

`Success` and `Cancelled` bills are never queried again; `--force` queries them anyway (blanks are still the only thing filled).

A bill is `Success` when IEC, exporter, invoice, LEO date, container, size and seal are known.
`Cancelled` comes from the portal's current status. Dates are stored as ISO and written as
native Excel dates.

Known limits:

- The gatepass appears only once the portal has it; until then size and seal stay blank.
- A gatepass with a non-standard container number or a conflicting identity is rejected, not guessed.
- A bill the portal answers nothing for stays `Not found` on every run.

## Safety

- All ICEGATE calls are read-only. Credentials stay in the `icegate` client.
- `report.xlsx` is written to a temporary file and renamed, so a failed run leaves the old one.
- `data/` (git-ignored) holds `report.db` (mode 0600).

## Development

```bash
bun test
bun check
```

Code is in `src/ts/`, tests in `tests-ts/`; rules and module boundaries are in
[AGENTS.md](AGENTS.md).
