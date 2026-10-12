import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from config import EXCEL_PATH, REQUIRED_DATA_FIELDS, require_openrouter_key
from excel_manager import (
    ExcelManager,
    build_merged_cell_map,
    coerce_excel_date,
    excel_cell_to_str,
    get_effective_cell_value,
    is_cancelled_value,
    is_empty_cell,
)
from extractor import extract_with_ai
from gmail_client import GmailClient
from leo_validation import resolve_leo_date
from models import CustomsExtractedFields, ExcelRow
from pdf_processor import split_source_pages, trim_pdf, verify_identity

console = Console()
app = typer.Typer(add_completion=False)


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
) -> CustomsExtractedFields:
    """
    Trims redundant pages across all available documents and extracts all customs fields
    in ONE single unified AI call per Shipping Bill.
    """
    if not doc_dict:
        return CustomsExtractedFields()

    priority = [
        ("eGatepass", "EGATEPASS", "Customs Gatepass (SB35)"),
        ("finalLeo", "FINAL_LEO", "Final LEO Shipping Bill (SB22)"),
        ("firstCopy", "FIRST_COPY", "First Copy Shipping Bill (SB21)"),
    ]

    combined_sections: list[str] = []
    valid_slots: set[str] = set()
    source_contexts: dict[str, dict[int, str]] = {}
    for slot, doc_type, label in priority:
        if slot not in doc_dict:
            continue
        filename, raw_bytes = doc_dict[slot]
        text, _kept_pages = trim_pdf(raw_bytes, doc_type)

        # Identity guard: skip PDFs that don't mention this shipping bill.
        if sb_no and not verify_identity(text, sb_no):
            console.print(f"  [yellow]⚠ {filename}: SB number {sb_no} not found in PDF text — skipped.[/yellow]")
            continue

        if text.strip():
            combined_sections.append(f"=== DOCUMENT: {label} ({filename}) ===\n{text.strip()}")
            valid_slots.add(slot)
            source_contexts[slot] = split_source_pages(text)

    if not combined_sections:
        return CustomsExtractedFields()

    combined_text = "\n\n".join(combined_sections)
    extracted = extract_with_ai(combined_text, doc_type="COMBINED", sb_no=sb_no)

    leo_result = resolve_leo_date(extracted.leo_evidence, source_contexts)
    extracted.leo_date = leo_result.leo_date
    extracted.leo_evidence = leo_result.accepted_evidence
    extracted.leo_warning = extracted.leo_warning or leo_result.warning
    if leo_result.warning:
        console.print(f"  [yellow]⚠ LEO date not applied: {leo_result.warning}[/yellow]")

    # Deterministic guards:
    # 1. LEO is only granted via validated Gatepass (SB35) or Final LEO (SB22) evidence.
    if "eGatepass" not in valid_slots and "finalLeo" not in valid_slots:
        extracted.leo_date = None

    # 2. Containers, sizes, and seals are ONLY present on Gatepass (Allowed for Movement table).
    if "eGatepass" not in valid_slots:
        extracted.container_numbers = None
        extracted.container_sizes = None
        extracted.customs_seal_numbers = None

    # 3. Invoice numbers are ONLY present on Shipping Bills (SB21/SB22). If neither is present, clear invoice_numbers.
    if "finalLeo" not in valid_slots and "firstCopy" not in valid_slots:
        extracted.invoice_numbers = None

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
        docs = gmail.fetch_documents(sb_clean)
        if not docs:
            console.print(f"  [yellow]Row {n} SB {sb_clean}[/yellow]: no ICEGATE mail in Gmail — skipped.")
            totals["no-docs"] += 1
            continue
        try:
            merged = process_documents(docs, sb_no=sb_clean)
        except Exception as e:
            console.print(f"  [red]Row {n} SB {sb_clean}: extraction failed: {e}[/red]")
            totals["MISMATCH"] += 1
            continue
        table = Table(title=f"Row {n} — SB {sb_clean} ({len(docs)} doc(s): {', '.join(sorted(docs))})")
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


def _fetch_and_extract_worker(
    row_idx: int,
    sb_no: str,
    missing_fields: list[str],
    gmail: GmailClient,
) -> tuple[int, str, list[str], dict[str, tuple[str, bytes]], CustomsExtractedFields | None, str | None]:
    """Worker executed concurrently across worker threads."""
    try:
        docs = gmail.fetch_documents(sb_no)
        if not docs:
            return (row_idx, sb_no, missing_fields, {}, None, None)
        extracted = process_documents(docs, sb_no=sb_no)
        return (row_idx, sb_no, missing_fields, docs, extracted, None)
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
):
    """Fetch, extract and write selected rows; --force includes filled rows."""
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
                    executor.submit(_fetch_and_extract_worker, row.row_idx, row.sb_no, row.missing_fields, gmail): row
                    for row in missing_rows
                }

                for completed_idx, future in enumerate(as_completed(future_to_row), start=1):
                    # Drop completed Futures so their PDF payloads can be freed.
                    row_item = future_to_row.pop(future)
                    try:
                        row_idx, sb_no, missing_fields, docs, extracted, error_str = future.result()
                        if error_str:
                            progress.console.print(f"  [bold red]❌ SB {sb_no} (Row {row_idx}): {error_str}[/bold red]")
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
                            if warning or ("leo_date" in missing_fields and not extracted.leo_date):
                                reason = warning or "no validated date in eligible documents"
                                status, detail = "Partial", "LEO unchanged: " + reason
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
                            excel_mgr.save()
                            progress.console.print(f"  [cyan]💾 Batch checkpoint: saved workbook ({completed_idx}/{len(missing_rows)} rows).[/cyan]")
    finally:
        excel_mgr.save()

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
    force: bool = typer.Option(False, "--force", help="Re-extract every selected row, including filled values"),
    leo_only: bool = typer.Option(False, "--leo-only", help="Write only LEO dates; combine with --force to repair filled dates"),
    rows: str | None = typer.Option(None, "--rows", help="Limit sync to Excel rows, e.g. '1349-1351' or '1349,1350,1351'"),
    verify_rows: str | None = typer.Option(None, "--verify-rows", help="Read-only audit: re-extract rows and diff vs filled Excel, e.g. '2-20'"),
    workers: int = typer.Option(3, "--workers", help="Number of concurrent worker threads (default 3, max 8)"),
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
        gmail = GmailClient()
        cmd_sync(
            excel_mgr,
            sheet,
            gmail,
            force=force,
            rows=parse_row_spec(rows) if rows else None,
            workers=workers,
            sb_filter=sb,
            leo_only=leo_only,
        )


def main():
    app()


if __name__ == "__main__":
    main()
