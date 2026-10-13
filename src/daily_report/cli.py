"""Command-line entry point for local ICEGATE parsing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from daily_report.gmail import GmailClient
from daily_report.rules import DATA_FIELDS, LEO_FIELDS
from daily_report.workbook import WorkbookStore
from daily_report.workflow import execute

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)
console = Console()


def parse_row_spec(spec: str) -> set[int]:
    rows: set[int] = set()
    for part in spec.split(','):
        bounds = part.strip().split('-')
        if not 1 <= len(bounds) <= 2 or any(not item.isascii() or not item.isdigit() for item in bounds):
            raise ValueError('Rows must be numbers or ascending ranges, e.g. 2,4-6')
        first, last = int(bounds[0]), int(bounds[-1])
        if not 2 <= first <= last <= 1_048_576:
            raise ValueError('Row ranges must be ascending within Excel data rows 2-1048576')
        rows.update(range(first, last + 1))
    return rows


def _show_check(rows, fields):
    table = Table(title='Missing-field audit (read-only)')
    for name in ('Row', 'SB', 'Missing fields'):
        table.add_column(name)
    missing = 0
    cancelled = 0
    errors = 0
    for row in rows:
        if row.error:
            errors += 1
            table.add_row(str(row.row_idx), row.identity.sb_no, 'Error: ' + row.error)
            continue
        if row.cancelled:
            cancelled += 1
            continue
        absent = [name for name in DATA_FIELDS if name in fields and not row.values.get(name)]
        if absent:
            missing += 1
            table.add_row(str(row.row_idx), row.identity.sb_no, ', '.join(absent))
    console.print(table)
    console.print(f'{missing} incomplete row(s); {cancelled} cancelled row(s); {errors} identity error(s); {len(rows)} selected row(s).')


def _show_records(records, verify: bool):
    table = Table(title='Read-only source audit' if verify else 'Sync outcome')
    for name in ('Row', 'SB', 'Status', 'Detail'):
        table.add_column(name)
    for row, result, update in records:
        table.add_row(str(row.row_idx), row.identity.sb_no, update.status, update.detail)
        if verify and result is not None:
            console.print(f'Row {row.row_idx}, SB {row.identity.sb_no}')
            for name, observation in sorted(result.fields.items()):
                current = row.values.get(name)
                value = '\n'.join(item or '' for item in observation.value) if isinstance(observation.value, list) else observation.value
                verdict = 'MATCH' if observation.state == 'accepted' and value == current else 'DIFFERENT' if observation.state == 'accepted' else observation.state.upper()
                console.print(f'  {name}: Excel={current!s}; source={value!s}; {verdict}')
                for evidence in observation.evidence:
                    console.print(f'    {evidence.filename}, page {evidence.page_number}, {evidence.label}: {evidence.quote}')
                if observation.reason:
                    console.print(f'    {observation.reason}')
    console.print(table)
    console.print(f'{len(records)} row(s) processed. ' + ('Workbook unchanged.' if verify else 'Validated updates saved.'))


@app.command()
def cli(
    file: Annotated[Path | None, typer.Option('--file', help='Workbook path.')] = None,
    sheet: Annotated[str, typer.Option(help='Exact sheet name.')] = 'FY 2026-27',
    check: Annotated[bool, typer.Option(help='Read-only missing-field audit; no Gmail calls.')] = False,
    sb: Annotated[str | None, typer.Option('--sb', help='Seven-digit Shipping Bill number.')] = None,
    rows: Annotated[str | None, typer.Option(help='Excel rows, e.g. 2,4-6.')] = None,
    verify_rows: Annotated[str | None, typer.Option(help='Read-only fresh local parsing of selected rows.')] = None,
    leo_only: Annotated[bool, typer.Option(help='Process SB22 LEO only.')] = False,
    force: Annotated[bool, typer.Option(help='Allow validated updates to filled selected cells.')] = False,
    refresh: Annotated[bool, typer.Option(help='Reparse stored PDFs instead of reusing parsed results.')] = False,
    workers: Annotated[int, typer.Option(min=1, max=8, help='Concurrent bills; Excel writes remain sequential.')] = 3,
    data_dir: Annotated[Path | None, typer.Option(help='Private per-SB storage root.')] = None,
):
    load_dotenv(Path.cwd() / '.env')
    try:
        if sb is not None and (len(sb) != 7 or not sb.isascii() or not sb.isdigit()):
            raise ValueError('--sb must contain exactly seven digits')
        if rows is not None and verify_rows is not None:
            raise ValueError('Use --rows or --verify-rows, not both')
        if check and verify_rows is not None:
            raise ValueError('--check and --verify-rows are separate audit modes')
        row_numbers = parse_row_spec(verify_rows if verify_rows is not None else rows) if rows is not None or verify_rows is not None else None
        workbook_path = file or Path(os.getenv('EXCEL_PATH', 'Daily Working Report.xlsx'))
        storage_root = data_dir or Path(os.getenv('DAILY_REPORT_DATA_DIR', 'data'))
        workbook = WorkbookStore(workbook_path)
        selected = workbook.rows(sheet, row_numbers=row_numbers, sb_no=sb)
        fields = set(LEO_FIELDS) if leo_only else set(DATA_FIELDS)
        if check:
            _show_check(selected, fields)
            return
        if not selected:
            console.print('No matching workbook rows.')
            return
        if not any(not row.cancelled and not row.error and (verify_rows is not None or force or any(not row.values.get(name) for name in fields)) for row in selected):
            # execute still stamps selected cancelled rows without accessing Gmail.
            console.print('No selected data fields need extraction.')
        gmail = GmailClient(Path(os.getenv('GMAIL_CREDENTIALS_FILE', 'credentials.json')),
                            Path(os.getenv('GMAIL_TOKEN_FILE', 'token.json')))
        records = execute(workbook, sheet, gmail, storage_root, row_numbers=row_numbers, sb_no=sb,
                          force=force, leo_only=leo_only, workers=workers, refresh=refresh,
                          verify=verify_rows is not None)
        _show_records(records, verify_rows is not None)
        if any(update.status == 'Error' for _, _, update in records):
            raise typer.Exit(code=1)
    except (ValueError, OSError) as exc:
        console.print(f'[red]Error:[/red] {exc}', markup=True)
        raise typer.Exit(code=1) from exc


def main():
    app()
