# AGENTS.md — daily-report-local

Standalone Python CLI (uv + typer + openpyxl + PyMuPDF + Pydantic v2 + OpenAI SDK via OpenRouter).
Fills `Daily Working Report.xlsx` in place from ICEGATE Gmail PDFs.
Package lives flat in `src/`; root `sync.py` is a shim.

## Essential Commands

```bash
uv run --no-sync sync.py --check                         # audit missing fields without modifying workbook
uv run --no-sync sync.py                                 # full sync: Gmail -> prune -> AI extract -> Excel
uv run --no-sync sync.py --sb 1278254                    # sync single 7-digit Shipping Bill
uv run --no-sync sync.py --rows 1349-1351                # sync only specific Excel rows
uv run --no-sync sync.py --verify-rows 2-20              # read-only audit: re-extract and diff vs filled Excel
uv run --no-sync pytest -q                               # unit tests (tests/test_*.py)
uv run --no-sync ruff check src/ tests/ sync.py          # lint (must be 100% clean)
```

## Ground Truth (Do Not Regress)

- **Pure AI Extraction**: 100% powered by OpenRouter (`meta/muse-spark-1.3-contributor`) with strict JSON schema. Zero regex parser fallback.
- **Shipper Name**: TRUE exporter entity name ONLY (from SB21/SB22 discharge line or Gatepass `4.CHA`). NEVER the CHA company name or `3.Exporter` block.
- **Invoice**: Primary invoice token from `2.INV NO.` in SB21/SB22. Exclude dates/amounts/currency.
- **LEO Date**: Gatepass order date first, SB22 second (`6.LEO Date` or `9.LEO` event). Saved as native `datetime.date`.
- **Containers/Seals/Sizes**: Gatepass `Allowed for Movement` table only. Multi-values joined by `\n`.
- **Status Column**: All FY sheets use `Status` column (`Success`, `Partial`, `Cancelled`, `Not found`).

## Critical Gotchas

- **OpenPyXL**: NEVER use `read_only=True` on `openpyxl.load_workbook()` (re-streaming causes multi-minute hangs).
- **Merged Cells**: Read-only `MergedCell` children (common in LCL containers) are mapped to parent top-left cells via `build_merged_cell_map()`.
- **Cancelled Bills**: Rows with `Billing Status == CANCELLED` are stamped as `Cancelled` and skipped to save API quota.
- **Concurrency**: `cmd_sync` runs parallel fetching via `ThreadPoolExecutor` (max 8 workers) with thread-local Gmail services and an `_auth_lock` protecting token refresh. Excel writes remain sequential.
- **Backups**: `ExcelManager.save()` writes `.bak-<YYYYMMDD-HHMMSS-ffffff>.xlsx` (newest 5 kept) before overwriting in place.
- **PyMuPDF**: Always keep raw block order; `page.get_text(sort=True)` regresses extraction. Documents must be closed in `finally:` blocks.
- **Pruning**: `trim_pdf` removes item lists and drawback claims, returning `tuple[str, list[int]]` (combined text and kept page indices).
- **Identity Guard**: `process_documents` verifies the 7-digit SB number is present in PDF text before extraction.
- **Secrets**: `.env`, `credentials.json`, `token.json`, and `*.xlsx` are strictly git-ignored. Never commit them.
