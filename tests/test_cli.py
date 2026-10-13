from pathlib import Path

import openpyxl
from typer.testing import CliRunner

from daily_report.cli import app, parse_row_spec


def workbook(path: Path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'FY 2026-27'
    sheet.append(['Shipping Bill No', 'Shipping Bill Date', 'Port Code', 'Shipper Name', 'Inv No',
                  'LEO Date', 'Container No', 'Container Size', 'Customs Seal', 'Billing Status'])
    sheet.append(['7546404', '07-10-2026', 'INMBD6', None, None, None, None, None, None, None])
    book.save(path)


def test_row_parser_rejects_malformed_or_header_scope():
    import pytest
    for spec in ['1', '3-2', '2,,3', 'foo', '2-1048577']:
        with pytest.raises(ValueError):
            parse_row_spec(spec)
    assert parse_row_spec('2,4-6') == {2, 4, 5, 6}


def test_check_is_read_only_and_does_not_construct_gmail(tmp_path, monkeypatch):
    path = tmp_path / 'report.xlsx'
    workbook(path)
    before = path.read_bytes()
    from daily_report import cli
    monkeypatch.setattr(cli, 'GmailClient', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Gmail forbidden')))
    result = CliRunner().invoke(app, ['--file', str(path), '--check'])
    assert result.exit_code == 0, result.output
    assert '7546404' in result.output
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.bak-*'))


def test_unknown_sheet_fails_without_modification(tmp_path):
    path = tmp_path / 'report.xlsx'
    workbook(path)
    before = path.read_bytes()
    result = CliRunner().invoke(app, ['--file', str(path), '--check', '--sheet', 'unknown'])
    assert result.exit_code != 0
    assert 'Unknown sheet' in result.output
    assert path.read_bytes() == before


def test_invalid_sb_does_not_broaden_selection(tmp_path):
    path = tmp_path / 'report.xlsx'
    workbook(path)
    result = CliRunner().invoke(app, ['--file', str(path), '--check', '--sb', 'oops'])
    assert result.exit_code != 0


def test_no_ai_options_exposed():
    result = CliRunner().invoke(app, ['--help'])
    assert result.exit_code == 0
    assert '--data-dir' in result.output
    assert 'OpenRouter' not in result.output
