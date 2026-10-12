# AGENTS.md — daily-report-local

Standalone Python CLI (uv + typer + openpyxl + AnyDoc + PyMuPDF + Pydantic v2 + OpenAI SDK via OpenRouter).
Fills `Daily Working Report.xlsx` in place from ICEGATE Gmail PDFs.
Package lives flat in `src/`; root `sync.py` is a shim.

## Essential Commands

```bash
uv run --no-sync sync.py --check                         # audit missing fields without modifying workbook
uv run --no-sync sync.py                                 # full sync: Gmail -> prune -> AI extract -> Excel
uv run --no-sync sync.py --sb 1278254                    # sync single 7-digit Shipping Bill
uv run --no-sync sync.py --rows 1349-1351                # sync only specific Excel rows
uv run --no-sync sync.py --verify-rows 2-20 --leo-only   # read-only LEO audit with source evidence
uv run --no-sync sync.py --force --leo-only             # repair all eligible LEO dates, including filled rows
uv run --no-sync pytest -q                               # unit tests (tests/test_*.py)
uv run --no-sync ruff check src/ tests/ sync.py          # lint (must be 100% clean)
```

## Ground Truth (Do Not Regress)

- **Pure AI Extraction**: 100% powered by OpenRouter (`meta/muse-spark-1.3-contributor`) with strict JSON schema. Zero regex parser fallback.
- **Shipper Name**: TRUE exporter entity name ONLY (from SB21/SB22 discharge line or Gatepass `4.CHA`). NEVER the CHA company name or `3.Exporter` block.
- **Invoice**: Primary invoice token from `2.INV NO.` in SB21/SB22. Exclude dates/amounts/currency.
- **LEO Date**: Gatepass order date first, SB22 second (`6.LEO Date` or `9.LEO` event). Saved as native `datetime.date`. AI evidence must match the original page, eligible label, and quoted source. Conflicting/unsupported evidence never overwrites LEO.
- **Containers/Seals/Sizes**: Gatepass `Allowed for Movement` table only. Multi-values joined by `\n`.
- **Status Column**: All FY sheets use `Status` column (`Success`, `Partial`, `Cancelled`, `Not found`; `Error` for runtime/API/conversion failures).

## Critical Gotchas

- **OpenPyXL**: NEVER use `read_only=True` on `openpyxl.load_workbook()` (re-streaming causes multi-minute hangs).
- **Merged Cells**: Read-only `MergedCell` children (common in LCL containers) are mapped to parent top-left cells via `build_merged_cell_map()`.
- **Cancelled Bills**: Rows with `Billing Status == CANCELLED` are stamped as `Cancelled` and skipped to save API quota.
- **Concurrency**: `cmd_sync` runs parallel fetching via `ThreadPoolExecutor` (max 8 workers) with thread-local Gmail services and an `_auth_lock` protecting token refresh. Excel writes remain sequential.
- **Backups**: `ExcelManager.save()` writes `.bak-<YYYYMMDD-HHMMSS-ffffff>.xlsx` (newest 5 kept) before overwriting in place.
- **AnyDoc**: Primary conversion is native Markdown, per retained page with original source page markers. OCR is off (`ocr="reject"`); raise a clear conversion error for pages requiring OCR. No hosted requests or raw-text fallback.
- **PyMuPDF**: Always keep raw block order; `page.get_text(sort=True)` regresses extraction. Documents must be closed in `finally:` blocks.
- **Pruning**: `trim_pdf` removes item lists and drawback claims, returning `tuple[str, list[int]]` (AnyDoc Markdown with source markers and zero-based kept page indices).
- **Selective Sources**: SB22 supersedes SB21. Download SB21 only for missing/unusable invoice/exporter information. LEO-only uses Gatepass/SB22 page one and requests only evidence.
- **LEO Recovery**: Search broadly only for missing LEO document types; validate broadly discovered PDF identity before use. Inspect older distinct versions lazily only after every newest eligible LEO observation is validated blank. Stop on rejection/conflict/conversion failure; never replace newest non-LEO fields with historical values. Print historical filenames for provenance.
- **Cache**: `.cache/daily-report` (or `DAILY_REPORT_CACHE_DIR`) stores private PDF/text/raw AI artifacts. Revalidate cached LEO evidence against current sources; never trust cached derived dates. Metadata discovery stays fresh. `--refresh` bypasses AI cache; `--no-cache` disables caching; audits always use fresh AI.
- **AI Cost Controls**: Well-formed full-result observations (including blanks/rejections) can seed LEO-only reuse for identical eligible PDFs after revalidation. Store unchanged source-bound blank/rejected results separately from successes; shared LEO observation aliases contain raw evidence only. Do not cache malformed responses/API/conversion errors as unresolved results. Report provider token/cost usage without estimating missing costs.
- **Identity Guard**: `process_documents` verifies the 7-digit SB number is present in PDF text before extraction.
- **Repair Scope**: `--force` includes complete rows; `--leo-only` restricts data writes to LEO. Status writes obey row/SB filters. Reject unknown sheet names. Never erase an existing date when evidence is missing or rejected.
- **Secrets**: Private caches, `.env`, `credentials.json`, `token.json`, and `*.xlsx` are strictly git-ignored. Never commit them.
