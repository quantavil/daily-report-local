import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from config import (
    CACHE_DIR,
    EXCEL_PATH,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    REQUIRED_DATA_FIELDS,
    require_openrouter_key,
)
from excel_manager import (
    ExcelManager,
    build_merged_cell_map,
    coerce_excel_date,
    excel_cell_to_str,
    get_effective_cell_value,
    is_cancelled_value,
    is_empty_cell,
)
from extractor import _build_prompt, extract_with_ai, get_strict_json_schema
from gmail_client import GmailClient
from leo_validation import resolve_leo_date
from models import CustomsExtractedFields, ExcelRow
from pdf_processor import PdfConversionError, split_source_pages, trim_pdf, verify_identity
from pipeline_cache import PipelineCache, cache_key

console = Console()
app = typer.Typer(add_completion=False)

def _package_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "unknown"


CONVERTER_VERSION = (
    "native-markdown-source-pages-v2",
    _package_version("firecrawl-anydoc"),
    _package_version("PyMuPDF"),
)
MODEL_VALIDATOR_VERSION = "customs-field-cleaners-v1"
LEO_VALIDATOR_VERSION = "source-page-evidence-validator-v1"
AI_CACHE_VERSION = "raw-ai-fields-v2"
_ALL_SYNC_FIELDS = frozenset(REQUIRED_DATA_FIELDS)
_GATEPASS_FIELDS = frozenset({
    "leo_date", "shipper_name", "container_no", "container_size", "customs_seal",
})
_SB22_FIELDS = frozenset({"inv_no", "leo_date", "shipper_name"})
_SB21_FIELDS = frozenset({"inv_no", "shipper_name"})


class PipelineMetrics:
    """Thread-safe stage totals and cache-hit counters for one sync run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seconds = {"gmail": 0.0, "conversion": 0.0, "ai": 0.0, "excel_save": 0.0}
        self._cache_hits = {"conversion": 0, "ai": 0}

    def add_seconds(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._seconds[stage] += seconds

    def cache_hit(self, stage: str) -> None:
        with self._lock:
            self._cache_hits[stage] += 1

    def summary(self, elapsed: float) -> str:
        with self._lock:
            seconds = dict(self._seconds)
            hits = dict(self._cache_hits)
        return (
            "Pipeline: elapsed {elapsed:.1f}s; worker time sums — Gmail {gmail:.1f}s, "
            "conversion {conversion:.1f}s ({conversion_hits} cache hit(s)), "
            "AI {ai:.1f}s ({ai_hits} cache hit(s)); Excel saves {excel_save:.1f}s"
        ).format(
            elapsed=elapsed,
            **seconds,
            conversion_hits=hits["conversion"],
            ai_hits=hits["ai"],
        )


def _normalize_requested_fields(fields: set[str] | None) -> set[str]:
    if fields is None:
        return set(_ALL_SYNC_FIELDS)
    normalized = set(fields)
    if "invoice_numbers" in normalized:
        normalized.add("inv_no")
    normalized.discard("invoice_numbers")
    return normalized & _ALL_SYNC_FIELDS


def _documents_for_fields(
    doc_dict: dict[str, tuple[str, bytes]],
    fields: set[str],
) -> dict[str, tuple[str, bytes]]:
    """Select only slots that can supply requested fields; SB22 wins over SB21."""
    selected: dict[str, tuple[str, bytes]] = {}
    if fields & _GATEPASS_FIELDS and "eGatepass" in doc_dict:
        selected["eGatepass"] = doc_dict["eGatepass"]
    if fields & _SB22_FIELDS and "finalLeo" in doc_dict:
        selected["finalLeo"] = doc_dict["finalLeo"]
    if fields & _SB21_FIELDS and "finalLeo" not in doc_dict and "firstCopy" in doc_dict:
        selected["firstCopy"] = doc_dict["firstCopy"]
    return selected


def _cached_conversion(
    raw_bytes: bytes,
    doc_type: str,
    *,
    leo_only: bool,
    cache: PipelineCache | None,
    metrics: PipelineMetrics | None,
) -> tuple[str, list[int]]:
    source_hash = cache_key("conversion-source-v1", raw_bytes, doc_type, leo_only, CONVERTER_VERSION)
    key = cache_key("conversion", raw_bytes, doc_type, leo_only, CONVERTER_VERSION)
    if cache is not None:
        cached = cache.get_json("conversion", key)
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("text"), str)
            and isinstance(cached.get("kept_pages"), list)
            and all(type(page) is int and page >= 0 for page in cached["kept_pages"])
            and cached.get("source_hash") == source_hash
            and cached.get("record_hash") == cache_key(
                "conversion-record-v1", source_hash, cached["text"], cached["kept_pages"]
            )
        ):
            if metrics:
                metrics.cache_hit("conversion")
            return cached["text"], list(cached["kept_pages"])

    started = time.perf_counter()
    text, kept_pages = trim_pdf(raw_bytes, doc_type, leo_only=leo_only)
    if metrics:
        metrics.add_seconds("conversion", time.perf_counter() - started)
    if cache is not None and text.strip():
        cache.put_json("conversion", key, {
            "text": text,
            "kept_pages": kept_pages,
            "source_hash": source_hash,
            "record_hash": cache_key("conversion-record-v1", source_hash, text, kept_pages),
        })
    return text, kept_pages


def _raw_ai_cache_key(
    text: str,
    sb_no: str,
    fields: set[str],
    source_identity: tuple[tuple[str, str, bytes], ...],
) -> str:
    prompt = _build_prompt("COMBINED", fields=fields)
    schema = get_strict_json_schema(fields=fields)
    return cache_key(
        AI_CACHE_VERSION,
        sb_no,
        text,
        source_identity,
        sorted(fields),
        OPENROUTER_MODEL,
        OPENROUTER_BASE_URL,
        prompt,
        schema,
        CONVERTER_VERSION,
        MODEL_VALIDATOR_VERSION,
        LEO_VALIDATOR_VERSION,
    )


def _read_cached_ai(cache: PipelineCache, key: str) -> CustomsExtractedFields | None:
    cached = cache.get_json("ai", key)
    if not isinstance(cached, dict):
        return None
    warning = cached.get("leo_warning")
    raw_fields = cached.get("fields")
    if warning or not isinstance(raw_fields, dict) or raw_fields.get("leo_warning"):
        return None
    if cached.get("record_hash") != cache_key("ai-cache-record-v1", raw_fields, warning):
        return None
    try:
        result = CustomsExtractedFields.model_validate(raw_fields)
    except Exception:
        return None
    result.leo_date = None
    result.leo_warning = warning
    return result


def _write_cached_ai(
    cache: PipelineCache,
    key: str,
    raw_fields: dict,
    warning: str | None,
) -> None:
    payload = deepcopy(raw_fields)
    payload["leo_date"] = None
    payload["leo_warning"] = warning
    cache.put_json("ai", key, {
        "fields": payload,
        "leo_warning": warning,
        "record_hash": cache_key("ai-cache-record-v1", payload, warning),
    })


def _target_sheet(excel_mgr: ExcelManager, sheet_name: str) -> str:
    sheets = excel_mgr.get_sheet_names()
    if sheet_name not in sheets:
        raise typer.BadParameter(f"Sheet {sheet_name!r} not found; available: {', '.join(sheets)}")
    return sheet_name


def _print_leo_evidence(extracted: CustomsExtractedFields) -> None:
    warning = getattr(extracted, "leo_warning", None)
    if warning:
        console.print(f"[yellow]LEO not verified: {warning}[/yellow]")
    for item in getattr(extracted, "leo_evidence", []):
        console.print(
            f"  {item.doc_type}, page {item.page_number}, {item.label}: {item.raw_date or '(blank)'}",
            markup=False,
        )
        console.print("    " + item.quote, markup=False)


def process_documents(
    doc_dict: dict[str, tuple[str, bytes]],
    sb_no: str = "",
    fields: set[str] | None = None,
    cache: PipelineCache | None = None,
    refresh: bool = False,
    metrics: PipelineMetrics | None = None,
) -> CustomsExtractedFields:
    """
    Selects only sources relevant to the requested workbook fields, converts them through
    native AnyDoc Markdown and uses one field-scoped AI call. Raw AI cache hits still pass
    through the current source-context LEO validator.
    """
    if not doc_dict:
        return CustomsExtractedFields()

    requested = _normalize_requested_fields(fields)
    selected_docs = _documents_for_fields(doc_dict, requested)
    if not selected_docs:
        return CustomsExtractedFields()

    leo_only = requested == {"leo_date"}
    priority = [
        ("eGatepass", "EGATEPASS", "Customs Gatepass (SB35)"),
        ("finalLeo", "FINAL_LEO", "Final LEO Shipping Bill (SB22)"),
        ("firstCopy", "FIRST_COPY", "First Copy Shipping Bill (SB21)"),
    ]

    combined_sections: list[str] = []
    valid_slots: set[str] = set()
    source_contexts: dict[str, dict[int, str]] = {}
    conversion_failures: set[str] = set()
    conversion_errors: list[str] = []
    for slot, doc_type, label in priority:
        if slot not in selected_docs:
            continue
        filename, raw_bytes = selected_docs[slot]
        try:
            text, _kept_pages = _cached_conversion(
                raw_bytes,
                doc_type,
                leo_only=leo_only,
                cache=cache,
                metrics=metrics,
            )
        except PdfConversionError as exc:
            conversion_failures.add(slot)
            conversion_errors.append(f"{slot}: {exc}")
            console.print(f"  [yellow]⚠ {filename}: conversion failed; source was skipped ({exc}).[/yellow]")
            continue

        if not text.strip():
            conversion_failures.add(slot)
            conversion_errors.append(f"{slot}: conversion produced no text")
            continue

        # Identity guard: skip PDFs that don't mention this shipping bill.
        if sb_no and not verify_identity(text, sb_no):
            console.print(f"  [yellow]⚠ {filename}: SB number {sb_no} not found in PDF text — skipped.[/yellow]")
            continue

        if text.strip():
            combined_sections.append(f"=== DOCUMENT: {label} ({filename}) ===\n{text.strip()}")
            valid_slots.add(slot)
            source_contexts[slot] = split_source_pages(text)

    if not combined_sections:
        empty = CustomsExtractedFields(conversion_errors=conversion_errors)
        if requested & {"leo_date"} and conversion_failures:
            empty.leo_warning = "LEO could not be checked because an eligible source failed conversion."
        return empty

    combined_text = "\n\n".join(combined_sections)
    source_identity = tuple(
        (slot, filename, raw_bytes)
        for slot, (filename, raw_bytes) in sorted(selected_docs.items())
    )
    ai_cache_key = _raw_ai_cache_key(combined_text, sb_no, requested, source_identity)
    raw_ai_fields: dict | None = None
    extracted = None
    if cache is not None and not refresh:
        extracted = _read_cached_ai(cache, ai_cache_key)
        if extracted is not None and metrics:
            metrics.cache_hit("ai")

    if extracted is None:
        started = time.perf_counter()
        extracted = extract_with_ai(
            combined_text,
            doc_type="COMBINED",
            sb_no=sb_no,
            fields=requested,
        )
        if metrics:
            metrics.add_seconds("ai", time.perf_counter() - started)
        # Preserve a pre-validation snapshot; the live result is filtered and its LEO
        # evidence is replaced with accepted evidence below.
        raw_ai_fields = extracted.model_dump(mode="json")
        raw_ai_fields["leo_warning"] = extracted.leo_warning
    raw_ai_warning = raw_ai_fields.get("leo_warning") if raw_ai_fields is not None else None
    extracted.conversion_errors = conversion_errors

    extracted.leo_date = None
    leo_warning = extracted.leo_warning
    if "leo_date" in requested:
        leo_result = resolve_leo_date(extracted.leo_evidence, source_contexts)
        extracted.leo_date = leo_result.leo_date
        extracted.leo_evidence = leo_result.accepted_evidence
        leo_warning = leo_warning or leo_result.warning
        failed_authoritative = conversion_failures & {"eGatepass", "finalLeo"}
        if failed_authoritative:
            failed_names = ", ".join(sorted(failed_authoritative))
            leo_warning = (
                f"LEO could not be checked because eligible source conversion failed: {failed_names}."
            )
            extracted.leo_date = None
        extracted.leo_warning = leo_warning
        if leo_warning:
            console.print(f"  [yellow]⚠ LEO date not applied: {leo_warning}[/yellow]")
    else:
        extracted.leo_date = None
        extracted.leo_evidence = []
        # Keep parser/runtime warnings visible for non-LEO extraction as well.
        extracted.leo_warning = leo_warning

    # Deterministic guards:
    # 1. LEO is only granted via validated Gatepass (SB35) or Final LEO (SB22) evidence.
    if "leo_date" in requested and "eGatepass" not in valid_slots and "finalLeo" not in valid_slots:
        extracted.leo_date = None

    # 2. Containers, sizes, and seals are ONLY present on Gatepass (Allowed for Movement table).
    if "eGatepass" not in valid_slots:
        extracted.container_numbers = None
        extracted.container_sizes = None
        extracted.customs_seal_numbers = None

    # 3. Invoice numbers are ONLY present on Shipping Bills (SB21/SB22). If neither is present, clear invoice_numbers.
    if "finalLeo" not in valid_slots and "firstCopy" not in valid_slots:
        extracted.invoice_numbers = None

    # Enforce the requested-field contract even if a provider/cache returned extra values.
    if "inv_no" not in requested:
        extracted.invoice_numbers = None
    if "shipper_name" not in requested:
        extracted.shipper_name = None
    if "leo_date" not in requested:
        extracted.leo_date = None
    if "container_no" not in requested:
        extracted.container_numbers = None
    if "container_size" not in requested:
        extracted.container_sizes = None
    if "customs_seal" not in requested:
        extracted.customs_seal_numbers = None

    # Do not treat malformed AI output or rejected/ambiguous LEO observations as a
    # successful cache result. A later run must ask the model again.
    if (
        cache is not None
        and raw_ai_fields is not None
        and not conversion_failures
        and not raw_ai_warning
        and not extracted.leo_warning
    ):
        _write_cached_ai(cache, ai_cache_key, raw_ai_fields, None)

    return extracted


def cmd_check(excel_mgr: ExcelManager, sheet_name: str):
    """Inspects Excel workbook and prints missing data summary."""
    console.print(Panel.fit(f"[bold cyan]Auditing Excel Workbook:[/bold cyan] {excel_mgr.file_path.name}"))
    sheets = excel_mgr.get_sheet_names()
    console.print(f"Available Sheets: [bold green]{', '.join(sheets)}[/bold green]\n")

    target_sheet = _target_sheet(excel_mgr, sheet_name)
    console.print(f"Scanning sheet: [bold yellow]{target_sheet}[/bold yellow]...")

    try:
        missing_rows, _col_map = excel_mgr.find_missing_rows(target_sheet)
    except ValueError as e:
        console.print(f"[bold red]❌ {e}[/bold red]")
        return

    table = Table(title=f"Missing Fields Summary for {target_sheet}")
    table.add_column("Row #", justify="right", style="cyan")
    table.add_column("Shipping Bill No", style="bold")
    table.add_column("SB Date", style="dim")
    table.add_column("Existing Shipper", style="magenta")
    table.add_column("Missing Fields", style="bold red")

    for r in missing_rows[:25]:
        date_str = r.sb_date.strftime("%d-%b-%Y") if r.sb_date else "N/A"
        table.add_row(
            str(r.row_idx),
            r.sb_no,
            date_str,
            (r.shipper_name or "")[:28],
            ", ".join(r.missing_fields)
        )

    console.print(table)
    if len(missing_rows) > 25:
        console.print(f"[dim]... and {len(missing_rows) - 25} more rows needing updates.[/dim]")
    console.print(f"\n[bold]Total rows needing data sync in {target_sheet}:[/bold] [red]{len(missing_rows)}[/red]")



def _norm_text(v: str | None) -> str:
    return re.sub(r"\s+", " ", (v or "").strip()).upper()


def _norm_seq(v: str | None) -> list[str]:
    return [t.strip().upper() for t in (v or "").replace(",", "\n").split("\n") if t.strip()]


def _norm_set(v: str | None) -> list[str]:
    return sorted(_norm_seq(v))


def _seq_verdict(ev: str, gv: str) -> tuple[str, str]:
    """match = identical order; order = same values, different sequence."""
    if _norm_seq(ev) == _norm_seq(gv):
        return ("match", gv)
    if _norm_set(ev) == _norm_set(gv):
        return ("order", gv)
    return ("MISMATCH", gv or "-")


def compare_audit(excel: dict[str, str], got: CustomsExtractedFields) -> list[tuple[str, str, str, str]]:
    """Diffs filled Excel values vs a fresh extraction.

    Returns (field, excel_val, extracted_val, verdict) where verdict is
    'match', 'order' (same multi-values, different sequence), 'MISMATCH',
    or '-' (Excel empty — nothing to check against).
    """
    def _leo_match(ev: str, gv: str) -> bool:
        if not ev.strip() or not gv.strip():
            return False
        ed = coerce_excel_date(ev)
        gd = coerce_excel_date(gv)
        if isinstance(ed, (date, datetime)) and isinstance(gd, (date, datetime)):
            return ed == gd
        return ev.strip().lower() == gv.strip().lower()

    checks = [
        ("shipper", excel["shipper"], got.shipper_name or "",
         _norm_text(excel["shipper"]) == _norm_text(got.shipper_name)),
        ("invoice", excel["invoice"], got.invoice_numbers or "",
         excel["invoice"].strip().lower() == (got.invoice_numbers or "").strip().lower()),
        ("leo", excel["leo"], got.leo_date or "",
         _leo_match(excel["leo"], got.leo_date or "")),
    ]
    out = []
    for field, ev, gv, same in checks:
        if not ev.strip():
            out.append((field, "-", gv or "-", "-"))
        elif gv and same:
            out.append((field, ev, gv, "match"))
        else:
            out.append((field, ev, gv or "-", "MISMATCH"))
    # Multi-value fields: exact order = match, same values shuffled = order.
    for field, attr in (("containers", got.container_numbers or ""),
                        ("sizes", got.container_sizes or ""),
                        ("seals", got.customs_seal_numbers or "")):
        ev = excel[field]
        if not ev.strip():
            out.append((field, "-", attr or "-", "-"))
            continue
        verdict, _ = _seq_verdict(ev, attr)
        out.append((field, ev, attr or "-", verdict))
    return out


def cmd_verify(
    excel_mgr: ExcelManager,
    sheet_name: str,
    gmail: GmailClient,
    rows: set[int],
    leo_only: bool = False,
):
    """Dry-run audit: re-extracts each row's SB from Gmail and diffs vs Excel.

    Read-only: never writes cells, never saves the workbook.
    """
    require_openrouter_key()
    target_sheet = _target_sheet(excel_mgr, sheet_name)
    console.print(Panel.fit(
        f"[bold cyan]Audit (read-only, workbook untouched):[/bold cyan] "
        f"{excel_mgr.file_path.name} | Sheet: [bold yellow]{target_sheet}[/bold yellow]"
    ))
    col_map = excel_mgr.resolve_columns(excel_mgr.wb[target_sheet])
    sb_col = col_map.get("sb_no", 0)
    if not sb_col:
        console.print("[red]No 'Shipping Bill No' column found.[/red]")
        return

    if not gmail.authenticate():
        console.print("[red]❌ Could not authenticate with Gmail.[/red]")
        return

    field_cols = {
        "shipper": col_map.get("shipper_name", 0),
        "invoice": col_map.get("inv_no", 0),
        "leo": col_map.get("leo_date", 0),
        "containers": col_map.get("container_no", 0),
        "sizes": col_map.get("container_size", 0),
        "seals": col_map.get("customs_seal", 0),
    }

    ws = excel_mgr.wb[target_sheet]
    merged_map = build_merged_cell_map(ws)
    totals = {"match": 0, "MISMATCH": 0, "order": 0, "-": 0, "no-docs": 0}
    for n in sorted(rows):
        if n < 2 or n > ws.max_row:
            console.print(f"[dim]Row {n}: beyond sheet data — skipped.[/dim]")
            continue
        sb_no = excel_cell_to_str(ws.cell(row=n, column=sb_col).value)
        sb_clean = re.sub(r"[^0-9]", "", sb_no)
        if len(sb_clean) != 7:
            console.print(f"[dim]Row {n}: not a 7-digit SB row — skipped.[/dim]")
            continue
        excel = {
            k: excel_cell_to_str(get_effective_cell_value(ws, n, c, merged_map)) if c else ""
            for k, c in field_cols.items()
        }
        requested = {"leo_date"} if leo_only else set(REQUIRED_DATA_FIELDS)
        row_result = _fetch_and_extract_worker(
            n,
            sb_clean,
            list(requested),
            gmail,
            cache=None,
            refresh=True,
        )
        _row_idx, _row_sb, _fields, selected_docs, merged, error = row_result
        if error:
            console.print(f"  [red]Row {n} SB {sb_clean}: extraction failed: {error}[/red]")
            totals["MISMATCH"] += 1
            continue
        if not selected_docs or merged is None:
            console.print(
                f"  [yellow]Row {n} SB {sb_clean}[/yellow]: "
                "No eligible ICEGATE documents for selected fields — skipped."
            )
            totals["no-docs"] += 1
            continue
        table = Table(title=f"Row {n} — SB {sb_clean} ({len(selected_docs)} doc(s): {', '.join(sorted(selected_docs))})")
        table.add_column("Field", style="cyan")
        table.add_column("Excel (filled)", style="bold")
        table.add_column("Fresh extract", style="magenta")
        table.add_column("Verdict", style="bold")
        for field, ev, gv, verdict in compare_audit(excel, merged):
            if leo_only and field != "leo":
                continue
            totals[verdict] = totals.get(verdict, 0) + 1
            color = {"match": "green", "MISMATCH": "red", "order": "yellow"}.get(verdict, "dim")
            table.add_row(field, ev, gv, f"[{color}]{verdict}[/{color}]")
        console.print(table)
        _print_leo_evidence(merged)

    console.print(
        f"\n[bold]Audit totals:[/bold] [green]{totals['match']} match[/green], "
        f"[red]{totals['MISMATCH']} MISMATCH[/red], [yellow]{totals['order']} same-values-wrong-order[/yellow], "
        f"[dim]{totals['-']} empty-in-Excel[/dim], "
        f"[yellow]{totals['no-docs']} rows with no Gmail docs[/yellow]."
    )


def parse_row_spec(spec: str) -> set[int]:
    """Parses '1349-1351', '1349,1350,1351' or '1349' into Excel row indices."""
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo, hi = part.split("-", 1)
                start, end = int(lo.strip()), int(hi.strip())
                if start < 2 or end < start:
                    raise ValueError
                wanted.update(range(start, end + 1))
            else:
                n = int(part)
                if n < 2:
                    raise ValueError
                wanted.add(n)
        except ValueError:
            raise typer.BadParameter(
                f"Invalid --rows segment {part!r}: use row numbers >= 2, "
                "e.g. '1349-1351' or '1349,1350,1351'"
            ) from None
    if not wanted:
        raise typer.BadParameter("Empty --rows spec: e.g. '1349-1351'")
    return wanted


def summarize_outcome(missing_before: list[str], applied: list[str], docs_found: bool) -> tuple[str, str]:
    """Classifies a synced row as Success / Partial / Not found + detail line."""
    if not docs_found:
        return ("Not found", "no ICEGATE mail in Gmail")
    applied_fields = {a.split("=", 1)[0] for a in applied}
    remaining = [f for f in missing_before if f not in applied_fields]
    if not remaining:
        return ("Success", f"{len(applied_fields)} field(s) filled")
    if applied_fields:
        return ("Partial", "still missing: " + ", ".join(remaining))
    return ("Not found", "nothing new extracted from PDFs")


def _warning_detail(
    warning: str,
    missing_fields: list[str],
    extracted: CustomsExtractedFields,
) -> str:
    """Keep parser failures distinct from an unverified LEO date in row status."""
    if "ai returned malformed" in warning.casefold():
        return "Extraction incomplete: " + warning
    if "leo_date" in missing_fields and not extracted.leo_date:
        return "LEO unchanged: " + warning
    return "Extraction incomplete: " + warning


def _fetch_and_extract_worker(
    row_idx: int,
    sb_no: str,
    missing_fields: list[str],
    gmail: GmailClient,
    cache: PipelineCache | None = None,
    refresh: bool = False,
    metrics: PipelineMetrics | None = None,
) -> tuple[int, str, list[str], dict[str, tuple[str, bytes]], CustomsExtractedFields | None, str | None]:
    """Worker executed concurrently across worker threads."""
    try:
        requested = _normalize_requested_fields(set(missing_fields))
        started = time.perf_counter()
        docs = gmail.fetch_documents(sb_no, fields=requested, include_first_copy=False)
        if metrics:
            metrics.add_seconds("gmail", time.perf_counter() - started)
        selected_docs = _documents_for_fields(docs, requested)
        extracted = (
            process_documents(
                selected_docs,
                sb_no=sb_no,
                fields=requested,
                cache=cache,
                refresh=refresh,
                metrics=metrics,
            )
            if selected_docs
            else CustomsExtractedFields()
        )

        fallback_fields = {
            field for field in requested & _SB21_FIELDS
            if not getattr(extracted, "invoice_numbers" if field == "inv_no" else "shipper_name")
        }
        if fallback_fields and "firstCopy" not in selected_docs:
            # A usable SB22 remains preferred. Fetch/conversion is widened only after its
            # requested invoice/exporter values are absent or the PDF was unusable. This
            # also covers bills where Gmail's initial profile returned no SB21 slot.
            try:
                first_copy = docs.get("firstCopy")
                if first_copy is None:
                    started = time.perf_counter()
                    fallback_docs = gmail.fetch_documents(
                        sb_no,
                        fields=fallback_fields,
                        first_copy_only=True,
                    )
                    if metrics:
                        metrics.add_seconds("gmail", time.perf_counter() - started)
                    first_copy = fallback_docs.get("firstCopy")
                if first_copy is not None:
                    fallback_result = process_documents(
                        {"firstCopy": first_copy},
                        sb_no=sb_no,
                        fields=fallback_fields,
                        cache=cache,
                        refresh=refresh,
                        metrics=metrics,
                    )
                    if "inv_no" in fallback_fields and fallback_result.invoice_numbers:
                        extracted.invoice_numbers = fallback_result.invoice_numbers
                    if "shipper_name" in fallback_fields and fallback_result.shipper_name:
                        extracted.shipper_name = fallback_result.shipper_name
                    # Keep the previously source-validated LEO result, while carrying any
                    # fallback extraction failure into the row status.
                    fallback_warning = fallback_result.leo_warning
                    if fallback_warning:
                        warnings = [
                            warning for warning in (extracted.leo_warning, fallback_warning)
                            if warning
                        ]
                        extracted.leo_warning = "; ".join(dict.fromkeys(warnings))
                    extracted.conversion_errors = list(dict.fromkeys(
                        [*extracted.conversion_errors, *fallback_result.conversion_errors]
                    ))
                    selected_docs["firstCopy"] = first_copy
            except Exception as e:
                return (row_idx, sb_no, missing_fields, selected_docs, extracted, str(e))

        if not selected_docs:
            return (row_idx, sb_no, missing_fields, {}, None, None)
        return (row_idx, sb_no, missing_fields, selected_docs, extracted, None)
    except Exception as e:
        return (row_idx, sb_no, missing_fields, {}, None, str(e))


def cmd_sync(
    excel_mgr: ExcelManager,
    sheet_name: str,
    gmail: GmailClient,
    force: bool = False,
    rows: set[int] | None = None,
    workers: int = 3,
    sb_filter: str | None = None,
    leo_only: bool = False,
    cache: PipelineCache | None = None,
    refresh: bool = False,
):
    """Fetch, extract and write selected rows; --force includes filled rows."""
    run_started = time.perf_counter()
    try:
        require_openrouter_key()
    except RuntimeError as e:
        console.print(f"[bold red]❌ {e}[/bold red]")
        raise typer.Exit(code=1) from e

    clean_filter = re.sub(r"[^0-9]", "", sb_filter) if sb_filter is not None else None
    if clean_filter is not None and len(clean_filter) != 7:
        raise typer.BadParameter("Shipping Bill number must be 7 digits")
    target_sheet = _target_sheet(excel_mgr, sheet_name)
    ws = excel_mgr.wb[target_sheet]
    col_map = excel_mgr.resolve_columns(ws)
    if not col_map.get("sb_no"):
        raise typer.BadParameter(f"No Shipping Bill No column in {target_sheet!r}")
    if leo_only and not col_map.get("leo_date"):
        raise typer.BadParameter(f"No LEO Date column in {target_sheet!r}")
    status_col = excel_mgr.ensure_status_column(target_sheet)
    col_map = excel_mgr.resolve_columns(ws)
    merged_map = build_merged_cell_map(ws)
    sb_col = col_map["sb_no"]
    billing_col = col_map.get("billing_status", 0)
    selected_fields = {"leo_date"} if leo_only else set(REQUIRED_DATA_FIELDS)
    outcomes: list[tuple[int, str, str, str]] = []
    missing_rows: list[ExcelRow] = []
    sb_found = False

    def missing_at(row_idx: int) -> list[str]:
        return [
            field for field in REQUIRED_DATA_FIELDS
            if col_map.get(field) and is_empty_cell(
                get_effective_cell_value(ws, row_idx, col_map[field], merged_map)
            )
        ]

    console.print(Panel.fit(
        f"[bold cyan]Starting {'LEO repair' if leo_only and force else 'sync'}:[/bold cyan] "
        f"{excel_mgr.file_path.name} | Sheet: [bold yellow]{target_sheet}[/bold yellow]"
    ))
    # One selection pass for normal, forced, row-filtered and SB-filtered runs.
    # Status writes obey the same scope as data writes.
    for n in range(2, ws.max_row + 1):
        if rows is not None and n not in rows:
            continue
        sb_no = re.sub(r"[^0-9]", "", excel_cell_to_str(
            get_effective_cell_value(ws, n, sb_col, merged_map)
        ))
        if len(sb_no) != 7 or (clean_filter is not None and sb_no != clean_filter):
            continue
        sb_found = True
        billing = get_effective_cell_value(ws, n, billing_col, merged_map) if billing_col else None
        if is_cancelled_value(billing):
            excel_mgr.write_status(target_sheet, n, status_col, "Cancelled")
            outcomes.append((n, sb_no, "Cancelled", "billing status is cancelled"))
            continue
        missing = missing_at(n)
        requested = [f for f in REQUIRED_DATA_FIELDS if f in selected_fields and col_map.get(f)]
        needed = requested if force else [f for f in missing if f in selected_fields]
        if not needed:
            previous = get_effective_cell_value(ws, n, status_col, merged_map)
            status = "Partial" if missing else "Success"
            detail = "already filled" if not missing else "still missing: " + ", ".join(missing)
            if not missing and previous in ("Partial", "Error", "Not found"):
                status, detail = previous, "previous result retained; re-extract with --force"
            excel_mgr.write_status(target_sheet, n, status_col, status)
            outcomes.append((n, sb_no, status, detail))
            continue
        sb_date_col = col_map.get("sb_date", 0)
        raw_sb_date = get_effective_cell_value(ws, n, sb_date_col, merged_map) if sb_date_col else None
        coerced = coerce_excel_date(raw_sb_date)
        parsed_date = coerced.date() if isinstance(coerced, datetime) else coerced if isinstance(coerced, date) else None
        missing_rows.append(ExcelRow(row_idx=n, sb_no=sb_no, sb_date=parsed_date, missing_fields=needed))

    if clean_filter is not None and not sb_found:
        console.print(f"[yellow]Shipping Bill {clean_filter} not found in the selected rows.[/yellow]")
    if not missing_rows:
        if outcomes:
            excel_mgr.save()
        console.print("[green]No selected cells need extraction. Use --force to re-extract filled values.[/green]")
        return

    worker_count = max(1, min(workers, 8))
    metrics = PipelineMetrics()
    console.print(f"Extracting [bold red]{len(missing_rows)}[/bold red] selected rows with [bold cyan]{worker_count} worker(s)[/bold cyan].")

    if not gmail.authenticate():
        console.print("[red]❌ Could not authenticate with Gmail. Place credentials.json in this folder to enable Gmail fetching.[/red]")
        return

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Syncing Shipping Bills ({worker_count} workers)...", total=len(missing_rows))

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_row = {
                    executor.submit(
                        _fetch_and_extract_worker,
                        row.row_idx,
                        row.sb_no,
                        row.missing_fields,
                        gmail,
                        cache,
                        refresh,
                        metrics,
                    ): row
                    for row in missing_rows
                }

                for completed_idx, future in enumerate(as_completed(future_to_row), start=1):
                    # Drop completed Futures so their PDF payloads can be freed.
                    row_item = future_to_row.pop(future)
                    try:
                        row_idx, sb_no, missing_fields, docs, extracted, error_str = future.result()
                        if error_str:
                            progress.console.print(f"  [bold red]❌ SB {sb_no} (Row {row_idx}): {error_str}[/bold red]")
                            if docs and extracted is not None:
                                excel_mgr.apply_updates(
                                    target_sheet, row_idx, extracted, col_map,
                                    force_overwrite=force, fields=selected_fields,
                                )
                            excel_mgr.write_status(target_sheet, row_idx, status_col, "Error")
                            outcomes.append((row_idx, sb_no, "Error", error_str))
                        elif not docs:
                            progress.console.print(f"  [yellow]SB {sb_no}[/yellow]: No matching ICEGATE emails found in Gmail.")
                            status, detail = summarize_outcome(missing_fields, [], docs_found=False)
                            excel_mgr.write_status(target_sheet, row_idx, status_col, status)
                            outcomes.append((row_idx, sb_no, status, detail))
                        else:
                            applied = excel_mgr.apply_updates(
                                target_sheet, row_idx, extracted, col_map,
                                force_overwrite=force, fields=selected_fields,
                            )
                            remaining = missing_at(row_idx)
                            warning = getattr(extracted, "leo_warning", None)
                            conversion_errors = getattr(extracted, "conversion_errors", [])
                            if conversion_errors:
                                status = "Error"
                                detail = "PDF conversion failed: " + "; ".join(conversion_errors)
                            elif warning:
                                status, detail = "Partial", _warning_detail(warning, missing_fields, extracted)
                            elif "leo_date" in missing_fields and not extracted.leo_date:
                                status, detail = "Partial", "LEO unchanged: no validated date in eligible documents"
                            elif remaining:
                                status, detail = "Partial", "still missing: " + ", ".join(remaining)
                            else:
                                status, detail = "Success", f"{len(applied)} field(s) filled"
                            excel_mgr.write_status(target_sheet, row_idx, status_col, status)
                            outcomes.append((row_idx, sb_no, status, detail))
                            if applied:
                                progress.console.print(f"  [green]✔ SB {sb_no} (Row {row_idx})[/green]: Updated {', '.join(applied)}")
                    except Exception as e:
                        progress.console.print(f"  [bold red]❌ SB {row_item.sb_no} (Row {row_item.row_idx}): {e}[/bold red]")
                        excel_mgr.write_status(target_sheet, row_item.row_idx, status_col, "Error")
                        outcomes.append((row_item.row_idx, row_item.sb_no, "Error", str(e)))
                    finally:
                        progress.advance(task)
                        if completed_idx % 20 == 0:
                            save_started = time.perf_counter()
                            excel_mgr.save()
                            metrics.add_seconds("excel_save", time.perf_counter() - save_started)
                            progress.console.print(f"  [cyan]💾 Batch checkpoint: saved workbook ({completed_idx}/{len(missing_rows)} rows).[/cyan]")
    finally:
        save_started = time.perf_counter()
        excel_mgr.save()
        metrics.add_seconds("excel_save", time.perf_counter() - save_started)

    console.print(f"[dim]{metrics.summary(time.perf_counter() - run_started)}[/dim]")

    table = Table(title="Sync Outcome")
    table.add_column("Row #", justify="right", style="cyan")
    table.add_column("Shipping Bill No", style="bold")
    table.add_column("Status", style="bold")
    table.add_column("Detail", style="dim")
    counts = {"Success": 0, "Partial": 0, "Cancelled": 0, "Not found": 0, "Error": 0}
    for row_idx, sb_no, status, detail in sorted(outcomes):
        counts[status] = counts.get(status, 0) + 1
        color = {
            "Success": "green",
            "Partial": "yellow",
            "Cancelled": "dim",
            "Error": "bold red",
        }.get(status, "red")
        table.add_row(str(row_idx), sb_no, f"[{color}]{status}[/{color}]", detail)
    console.print(table)

    summary_parts = [
        f"[green]{counts['Success']} success[/green]",
        f"[yellow]{counts['Partial']} partial[/yellow]",
    ]
    if counts["Cancelled"] > 0:
        summary_parts.append(f"[dim]{counts['Cancelled']} cancelled[/dim]")
    summary_parts.append(f"[red]{counts['Not found']} not found[/red]")
    if counts["Error"] > 0:
        summary_parts.append(f"[bold red]{counts['Error']} error(s)[/bold red]")

    console.print(
        "\n[bold green]🎉 Sync completed![/bold green] "
        + ", ".join(summary_parts)
        + f" in {excel_mgr.file_path.name}."
    )


@app.command()
def cli(
    file: str = typer.Option(str(EXCEL_PATH), "--file", help="Path to Daily Working Report.xlsx"),
    sheet: str = typer.Option("FY 2026-27", "--sheet", help="Sheet name (defaults to FY 2026-27)"),
    check: bool = typer.Option(False, "--check", help="Audit missing fields in Excel without making changes"),
    sb: str | None = typer.Option(None, "--sb", help="Filter for single 7-digit Shipping Bill number"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Include filled rows and update selected fields; compatible AI cache may be reused",
    ),
    leo_only: bool = typer.Option(
        False,
        "--leo-only",
        help="Extract and write only LEO dates; combine with --force to repair filled dates",
    ),
    rows: str | None = typer.Option(None, "--rows", help="Limit sync to Excel rows, e.g. '1349-1351' or '1349,1350,1351'"),
    verify_rows: str | None = typer.Option(None, "--verify-rows", help="Read-only audit: re-extract rows and diff vs filled Excel, e.g. '2-20'"),
    workers: int = typer.Option(3, "--workers", help="Number of concurrent worker threads (default 3, max 8)"),
    refresh: bool = typer.Option(False, "--refresh", help="Re-extract with AI instead of reusing cached AI output"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable attachment, conversion, and AI caches for this run"),
):
    """Direct Local Excel Sync for Indian ICEGATE Shipping Bills."""
    excel_path = Path(file)
    if not excel_path.exists():
        console.print(f"[bold red]Excel file not found at:[/bold red] {excel_path}")
        raise typer.Exit(1)

    excel_mgr = ExcelManager(excel_path)

    if check:
        cmd_check(excel_mgr, sheet)
    elif verify_rows:
        gmail = GmailClient()
        cmd_verify(excel_mgr, sheet, gmail, parse_row_spec(verify_rows), leo_only=leo_only)
    else:
        cache = None if no_cache else PipelineCache(CACHE_DIR)
        gmail = GmailClient(cache=cache)
        cmd_sync(
            excel_mgr,
            sheet,
            gmail,
            force=force,
            rows=parse_row_spec(rows) if rows else None,
            workers=workers,
            sb_filter=sb,
            leo_only=leo_only,
            cache=cache,
            refresh=refresh,
        )


def main():
    app()


if __name__ == "__main__":
    main()
