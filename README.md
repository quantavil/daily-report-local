# Daily Report Sync

Creates every shipping bill from ICEGATE into a local SQLite database (`data/report.db`),
fetches what each bill lacks, then rewrites `report.xlsx` from the database: one sheet per
financial year (`FY 2023-24` … `FY 2026-27`), oldest bill first. The database is the product;
the report is regenerated from it every run. Gmail provides a read-only gatepass fallback;
there is no browser automation, AI, OCR or manual data entry in the application.

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
bun run sync stuffing-set SBNO DATE "Factory stuffing" # override the seal rule for one bill (or "ICD stuffing")
bun run sync mark-manual SBNO DATE # close a bill by hand: never queried again
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
   Only bills these lists show for the logged-in ICEGATE ID are found: the Job Status report returns just the portal's latest ~200 rows, and the eGatepass list has no bill filed under another login. Searching in smaller windows returns the same bills.
2. The public SB enquiry fills LEO date, job, invoices and EGM containers.
3. The eGatepass PDF fills container, size, seal, the IEC (`IEC/Br`) and the exporter. If the ICEGATE gatepass is unavailable or cannot be read, Gmail is searched by SB number for PDF attachments, first in the bill’s financial year and then in later mail. Each candidate must pass the same bill number, date and port checks. Each accepted PDF is kept in `data/gatepass/` (git-ignored) and re-parsed from there. A gatepass listed with a placeholder SB date (mostly 2024; such a record has no IRN to download) is found in Gmail the same way, and its bill takes the SB date printed on the PDF.

Each sync reports gatepass counts for ICEGATE, Gmail, saved PDFs, unavailable documents and errors, and lists the result beside each processed bill. Counts describe the current run; the original source of a reused saved PDF is not recorded.

The Gmail fallback uses the existing `gws` CLI and a Gmail-authorized account. It reads messages and attachments without modifying mail. If `gws` or authorization is unavailable, the bill remains partial with a note; `--check` and `--excel` make no Gmail calls.

## Own job and agent email from Gmail

For bills dated from the run's `--from` with a LEO date and no cancellation, each sync reads Gmail once per bill (read-only, nothing downloaded): the threads that mention the SB number, their attachment file names, and the To addresses of the owner's own mail that carries an attachment.

- One job token (`EXP-yyyy-n`, or `VIDE-EXP-yyyy-n`) in a file name, e.g. `CheckList-EXP-2627-70.pdf`: `Y`.
- Several different tokens: the own-job cell reads `CHECK: EXP-2627-70, EXP-2627-71` so you can settle it by typing `Y` or `N`.
- No token, but mail about the bill exists: `N`. No mail: blank.
- Agent email: the outside address (not the owner's, not ICEGATE) that appears most often in To. If addresses tie, all of them are written, comma-separated, for you to delete the wrong one. If To holds no outside address (a reply addressed to the owner's own mailbox), Cc is used the same way; blank if there are none.

Values go into the `notes` table like typed remarks, only where nothing is stored. Type over a cell and your value wins; clear it and it stays blank (the bill is not read again). If Gmail is unavailable the step stops with one warning and the bills are tried on the next run. `--check` and `--excel` make no Gmail calls.

## The report

Columns are `export-map.json` (default below; edit in the dashboard or by hand). The file is
rebuilt from the database each run, so never edit it by hand.

| Column | Source |
| --- | --- |
| Shipping Bill Date / No, Port Code / ICD Code | bill identity |
| LEO Date | public SB enquiry |
| SB Job No | `sb_job_no`: `jobNumber` of the SB e-copy search row, the daily job counter at filing. The long Job Status / enquiry job number stays in the database (`job_no`) but is not exported. |
| Job Date | Job Status report, then the enquiry |
| Type of Stuffing (derived) | From the container seals, never stored: every seal carries a letter (RFID bolt seals such as BOLT, KLIK, ENOS) is `Factory stuffing`. Numeric, mixed or missing seals stay blank (a numeric seal is mostly ICD but can be Factory), unless set with `stuffing-set`. |
| Facilitation Category | Gatepass `14.Facilitation Category` (e.g. GREEN CHANNEL). |
| Remark(Agent email) | The one outside address the owner wrote to on mail carrying an attachment for the bill (see "Own job and agent email from Gmail"). Blank when there are none; tied addresses are all written. |
| Remark(Bill no.) | Typed by hand in Excel. Each sync reads them back into the database (table `notes`) before rewriting the file, so they last (see below). |
| Remark(Own job Y/N) | Filled from Gmail (below), otherwise typed by hand in Excel; either way kept like the other remarks: `Y` when the owner's own firm made the job, `N` when a third party did. The dropdown allows only Y or N, and a `Y` colours that row's `SB Job No` cell green through an Excel rule that follows what you type. |
| Exporter | IEC name (My IEC or `iec-set`), else the name other gatepasses gave that IEC (if they agree), else the gatepass `3.Exporter`, else the SB e-copy exporter (read only when IEC or exporter is still blank; filed bills have one before LEO). My IEC covers importers, not export clients, so it fills few names. |
| IEC | Job Status report, else the gatepass `IEC/Br` |
| Inv No | public SB enquiry (the invoice date stays in the database, not in the report) |
| Container No. / Size / Customs Seal Number | EGM list in the enquiry, then the gatepass |
| EGM No / EGM Date | public SB enquiry, per container |
| Type Shipment (derived) | `LCL` when a container is shared by bills of different exporters dated within 14 days, else blank. Derived columns have an amber header: you may type over them and the value lasts |
| Current Status | the portal's own status text |
| Status | `Success`, `Partial`, `Cancelled`, `Manual` or `Not found` |

The sheets follow the old daily report: dark-blue header, alternating white and pale-grey rows, thin borders, frozen header, filter, green tabs, widths fitted to the content.

`Success`, `Cancelled` and `Manual` bills are never queried again; `--force` queries them anyway. A new non-empty portal status replaces the previous status; other fields only fill blanks.

A bill is `Success` when IEC, exporter, invoice, LEO date, container, size and seal are known.
`Cancelled` is derived from the portal's current status, including `Purge after 30 days`.
`Manual` is a bill closed by hand with `mark-manual` because a field cannot be obtained; it keeps its gaps and shows `Manual`. `Partial` means a known bill is missing one or more required fields; `Not found` means no
successful Job Status or enquiry lookup has confirmed it. Status is calculated from the
stored fields each time, and run errors are reported separately rather than stored as a status.
Dates are stored as ISO and written as
native Excel dates.

Known limits:

- When neither ICEGATE nor Gmail has a matching, readable gatepass, its missing fields stay blank.
- A gatepass with a non-standard container number or a conflicting identity is rejected, not guessed.
- A bill the portal answers nothing for stays `Not found` on every run.

## Not stored in the database

`report.xlsx` is rebuilt from `data/report.db` on every sync, so anything the database does not hold is lost on the next sync.

- **Derived columns are computed, but a value you type over lasts:** `Status`, `Type of Stuffing (derived)` and `Type Shipment (derived)` are recomputed on every sync unless you typed into the cell; your value then replaces the computed one, and clearing the cell brings the computed value back. A typed `Status` changes only the report, not which bills are fetched. `stuffing-set` stores a stuffing type from the command line.
- **Kept in the database on purpose:** bills, invoices and containers; `iec_names` (set with `iec-set`); `notes` (typed remarks, typed-over derived values and the Gmail own-job and agent-email values; `stuffing-set` writes one); `mail_checked` (bills whose Gmail evidence was read); `manual` (set with `mark-manual`).
- **Typed values last:** `Remark(Bill no.)`, `Remark(Own job Y/N)` and any derived column you type over are stored in the database (table `notes`). Before each rewrite the sync reads `report.xlsx` back and keeps every cell you changed since the last write; clearing a cell clears the note. Save and close the file before you sync, because the sync reads what is on disk. Bills are matched by Shipping Bill No and Date, so keep those two columns in `export-map.json`. A workbook that cannot be read stops the run and is not overwritten.
- **Backup:** the file a sync replaces is first copied to `report.xlsx.bak` (git-ignored); the database also has git history.

## Trimming git history

`bun run purge-history` shows how many commits are older than 30 days; `--apply` squashes them into one baseline commit and force-pushes (`--days N` changes the cutoff). No backup is kept. The user timer `purge-history.timer` (`~/.config/systemd/user/`) runs it weekly with `--apply`; it skips a run if the working tree is not clean.

## Safety

- All ICEGATE calls are read-only. Credentials stay in the `icegate` client.
- `report.xlsx` is written to a temporary file and renamed, so a failed run leaves the old one, and the replaced file is kept as `report.xlsx.bak`.
- `data/` holds `report.db` (mode 0600, committed on purpose); its `gatepass/` folder of saved PDFs is git-ignored.

## Development

```bash
bun test
bun check
```

Code is in `src/ts/`, tests in `tests-ts/`; rules and module boundaries are in
[AGENTS.md](AGENTS.md).
