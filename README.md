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
2. Discover Gmail attachment metadata and choose the newest revision of each needed type. Use SB22 Final LEO and SB35 Gatepass first; fetch SB21 First Copy only as an invoice/exporter fallback. For missing LEO document types, widen the search to attachments anywhere in Gmail, including Spam/Trash. Canonical ICEGATE filenames identify the document type even in forwarded mail; mismatching SB filenames are excluded and broadly discovered PDFs must pass a page-one SB identity check.
3. Reuse unchanged PDF bytes and native conversions from the private cache. Select pages with unsorted PyMuPDF text and convert them to AnyDoc Markdown with original page numbers. LEO-only retains Gatepass/SB22 page one.
4. Request only selected fields using a strict JSON schema. Reuse matching cached AI observations; otherwise make one combined OpenRouter call. Well-formed full-extraction LEO observations, including blanks/rejections, can also satisfy a later LEO-only request for the exact same eligible PDFs after revalidation. Missing invoice/exporter information can require one extra scoped SB21 call. Extraction remains AI-powered; there is no regex parser fallback.
5. Validate fresh and cached AI-selected LEO evidence against the current document, page, label, and exact quotation. Reject unsupported or conflicting dates; never trust a cached derived date alone.
   If every latest eligible LEO field is explicitly verified blank, lazily inspect older distinct versions using the same AI/evidence validator. Stop at the first validated date or a rejected/conflicting/unreadable version. Historical recovery changes only LEO; invoice/exporter/container values retain their newest-source extraction. The console identifies earlier source filenames used.
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

# Process only LEO, including filled rows; compatible cached evidence can be reused
uv run --no-sync sync.py --sb 3924868 --force --leo-only

# Process LEO for every eligible row in the selected sheet
uv run --no-sync sync.py --sheet "FY 2026-27" --force --leo-only

# Require fresh AI while retaining byte/conversion caches
uv run --no-sync sync.py --sb 3924868 --force --leo-only --refresh

# Disable all persistent caching for this run
uv run --no-sync sync.py --sb 3924868 --force --leo-only --no-cache
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
| `--force` | Include filled rows and allow selected values to be updated; compatible cache results can be reused |
| `--refresh` | Bypass cached AI observations; retain byte/conversion reuse |
| `--no-cache` | Disable persistent cache reads and writes |
| `--workers N` | Concurrent fetch/extraction workers; default 3, maximum 8 |

`--force` without `--leo-only` may overwrite all six data fields. Row and SB filters apply to status writes too. Skipping filled cells preserves a previous rejection/error status until re-extraction. With `--leo-only`, other data fields remain untouched; overall row status can still be `Partial` if they are missing.

## Cache and incremental processing

The default cache is `.cache/daily-report`; set `DAILY_REPORT_CACHE_DIR` to another dedicated private directory. Directories/files are written with permissions 0700/0600. Cached artifacts contain business document data; keep custom cache paths outside Git.

Gmail metadata is searched fresh for each selected bill. Attachment identity reuses PDF downloads; content, profile, model, prompt, schema and processing versions determine conversion/AI reuse. Revised selected documents invalidate extraction. Corrupt entries are misses; storage failures disable caching without failing sync.

Normal sync requests missing fields. When SB21 arrives first, invoice/exporter can be filled provisionally; later SB22/Gatepass supply pending fields. Filled fields are reconsidered with `--force`; `--refresh` additionally requires fresh AI. Read-only verification always uses fresh AI.

Unchanged source-bound blank/rejected LEO observations are cached separately from successful results and are revalidated on every reuse. They cannot supply a date until validation accepts it. Changed PDFs, model/prompt/converter/validator changes, `--refresh`, or `--no-cache` trigger fresh extraction. Malformed AI responses, temporary API failures and conversion failures are not cached as unresolved results. Full-result caches seed narrower LEO reuse after validation; no cached derived date is trusted.

Stage seconds are accumulated across concurrent workers and can exceed wall time. Cache-hit counts cover conversion and AI; attachment hits are not counted. The summary also shows successful API response count, returned input/output tokens, and reported cost in USD with the number of responses supplying cost. Missing cost is not estimated; reported totals can be partial and exclude failures without usage. This phase retains complete LEO page-one context and one combined request for selected fields. A Gmail history cursor, aggressive cropping, lower reasoning settings and a local parser are not implemented.

## Development

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check src/ tests/ sync.py
```

The package lives flat in `src/`; root `sync.py` is a CLI shim. Tests use synthetic data and mocked external boundaries. Private PDFs and workbooks do not belong in commits.

## Troubleshooting

- **Missing OpenRouter key:** set it privately in `.env`, then rerun.
- **No matching email:** check the SB and expected ICEGATE subjects. Gmail checks result pages and selects the newest message revision per requested type.
- **OCR required:** inspect the source PDF; the native conversion path intentionally does not OCR it.
- **LEO not verified:** inspect the evidence in `--verify-rows`; unsupported evidence or conflicting dates are not written.
- **Wrong existing date:** normal sync preserves filled cells. Use the audit, then a scoped `--force --leo-only` repair; add `--refresh` to require fresh AI.

[MIT License](LICENSE).
