# Daily Report Sync

Python CLI that fills `Daily Working Report.xlsx` from ICEGATE PDF attachments in Gmail. It uses AnyDoc for native PDF-to-Markdown conversion, PyMuPDF for page selection, OpenRouter for AI extraction, and openpyxl for Excel writes.

## Setup

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Set `OPENROUTER_API_KEY` in `.env`. The default model is `meta/muse-spark-1.3-contributor`; `OPENROUTER_MODEL` can override it. Save a Google OAuth Desktop client as `credentials.json` and enable the Gmail API. The first Gmail run opens consent and saves `token.json`. Gmail access is read-only.

`.env`, credentials, tokens, and Excel workbooks are ignored by Git.

## Workflow

1. Select valid seven-digit Shipping Bill rows from the named sheet. Skip cancelled bills.
2. Fetch ICEGATE SB35 Gatepass, SB22 Final LEO, and SB21 First Copy attachments.
3. Select relevant PDF pages with unsorted PyMuPDF text. Convert those pages locally to AnyDoc Markdown, retaining original page numbers.
4. Make one OpenRouter call per SB using a strict JSON schema. Extraction remains AI-powered; there is no regex parser fallback.
5. Validate AI-selected LEO evidence against its document, page, label, and exact supporting quotation. Reject unsupported or conflicting dates.
6. Write selected cells sequentially. Preserve formulas and non-empty cells unless forced. Save a timestamped backup before each workbook save; retain the latest five backups.

**OCR is off.** A selected page that needs OCR produces a clear error; no hosted OCR request or raw-text conversion fallback occurs.

## Field sources

| Field | Source / rule |
|---|---|
| Shipper | True exporter from the SB21/SB22 discharge line or Gatepass `4.CHA` context; never the CHA company or `3.Exporter` block |
| Invoice | Primary token from SB21/SB22 `2.INV NO.`; exclude dates, amounts, and currency |
| LEO | Gatepass `1.Order No / date` first, SB22 `6.LEO Date` / `9.LEO` second; never SB21 |
| Containers, sizes, seals | Gatepass `Allowed for Movement` table; multiple values joined by newlines |

LEO is stored as a native Excel date. A date merely present elsewhere in a quote is insufficient: it must belong to the cited LEO/order label. Conflicting accepted dates leave the existing LEO cell unchanged and flag the row.

`Success` describes field completeness after processing, not independent certification of every field. `Partial` includes missing fields or rejected LEO evidence. `Cancelled` rows are skipped. `Not found` indicates no matching documents. `Error` indicates a conversion/API/runtime failure.

## Commands

Default sheet: `FY 2026-27`. An unknown sheet is rejected rather than silently selecting another sheet.

```bash
# Missing-field audit: no Gmail or AI calls, no workbook changes
uv run --no-sync sync.py --check

# Fill missing fields
uv run --no-sync sync.py

# Limit by SB, Excel row numbers, or financial year
uv run --no-sync sync.py --sb 3924868
uv run --no-sync sync.py --rows 512-514 --sheet "FY 2026-27"

# Read-only comparison against fresh AI extraction; prints LEO evidence
uv run --no-sync sync.py --verify-rows 512-514 --leo-only

# Repair only LEO, including rows that are already filled
uv run --no-sync sync.py --sb 3924868 --force --leo-only

# Re-extract LEO for every eligible row in the selected sheet
uv run --no-sync sync.py --sheet "FY 2026-27" --force --leo-only
```

Audit representative rows before a bulk repair. Auditing consumes Gmail/OpenRouter calls, but never writes the workbook. A matching fresh AI result alone is not independent proof; inspect the cited PDF evidence.

| Option | Effect |
|---|---|
| `--file PATH` | Workbook path; defaults to `Daily Working Report.xlsx`, overridable by `EXCEL_PATH` |
| `--sheet NAME` | Exact target sheet |
| `--check` | Missing-field audit only |
| `--sb NUMBER` | Select matching seven-digit SB rows |
| `--rows SPEC` | Row selection, e.g. `513`, `512-514`, or `512,514` |
| `--verify-rows SPEC` | Read-only re-extraction and comparison |
| `--leo-only` | Restrict sync writes or audit comparison to LEO |
| `--force` | Re-extract all selected eligible rows, including complete rows; overwrite selected non-empty fields |
| `--workers N` | Concurrent fetch/extraction workers; default 3, maximum 8 |

`--force` without `--leo-only` may overwrite all six data fields. Row and SB filters apply to status writes too. Skipping filled cells preserves a previous rejection/error status until re-extraction. With `--leo-only`, other data fields remain untouched; overall row status can still be `Partial` if they are missing.

## Development

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check src/ tests/ sync.py
```

The package lives flat in `src/`; root `sync.py` is a CLI shim. Tests use synthetic data and mocked external boundaries. Private PDFs and workbooks do not belong in commits.

## Troubleshooting

- **Missing OpenRouter key:** set it privately in `.env`, then rerun.
- **No matching email:** check the SB and expected ICEGATE subjects. Gmail currently selects the first encountered attachment of each type from up to 20 search results; it does not compare revisions or paginate.
- **OCR required:** inspect the source PDF; the native conversion path intentionally does not OCR it.
- **LEO not verified:** inspect the evidence in `--verify-rows`; unsupported evidence or conflicting dates are not written.
- **Wrong existing date:** normal sync preserves filled cells. Use the audit, then a scoped `--force --leo-only` repair.

[MIT License](LICENSE).
