"""Regression coverage for selecting and repairing only LEO cells."""
from datetime import date, datetime
from unittest.mock import patch

import openpyxl

from excel_manager import ExcelManager
from models import CustomsExtractedFields
from sync import cmd_sync

HEADERS = ["Shipping Bill No", "Inv No", "LEO Date", "Shipper Name", "Container No.",
           "Container Size", "Customs Seal Number", "Billing Status", "Status"]
ORIGINAL = ["3924868", "INV-OLD", date(2026, 6, 6), "ORIGINAL SHIPPER", "TCKU7809338",
            "40", "SEAL-OLD", None, None]


def workbook(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FY 2026-27"
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    path = tmp_path / "repair.xlsx"
    wb.save(path)
    manager = ExcelManager(path)
    manager.load()
    return manager


class Gmail:
    def __init__(self):
        self.fetched = []

    def authenticate(self):
        return True

    def fetch_documents(self, sb):
        self.fetched.append(sb)
        return {"finalLeo": ("sb22.pdf", b"external-boundary-placeholder")}


def extracted():
    return CustomsExtractedFields(
        invoice_numbers="INV-NEW", leo_date="08-Jun-2026", shipper_name="NEW SHIPPER",
        container_numbers="UNSU0088111", container_sizes="20", customs_seal_numbers="SEAL-NEW",
    )


def test_force_without_filter_selects_complete_rows(tmp_path, monkeypatch):
    """Regression: --force must include complete rows, not just missing rows."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    manager = workbook(tmp_path, [ORIGINAL])
    gmail = Gmail()
    with patch("sync.process_documents", return_value=extracted()):
        cmd_sync(manager, "FY 2026-27", gmail, force=True, workers=1)
    assert gmail.fetched == ["3924868"]
    loaded = openpyxl.load_workbook(manager.file_path)
    assert loaded.active.cell(2, 3).value == datetime(2026, 6, 8)


def test_leo_only_force_preserves_other_fields(tmp_path, monkeypatch):
    """The AI can return all six fields; only LEO may be overwritten."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    manager = workbook(tmp_path, [ORIGINAL])
    gmail = Gmail()
    with patch("sync.process_documents", return_value=extracted()):
        cmd_sync(manager, "FY 2026-27", gmail, force=True, leo_only=True, workers=1)
    loaded = openpyxl.load_workbook(manager.file_path)
    assert loaded.active.cell(2, 3).value == datetime(2026, 6, 8)
    assert [loaded.active.cell(2, c).value for c in [2, 4, 5, 6, 7]] == [
        "INV-OLD", "ORIGINAL SHIPPER", "TCKU7809338", "40", "SEAL-OLD",
    ]
    assert list(tmp_path.glob("repair.bak-*.xlsx"))


def test_leo_only_does_not_fetch_rows_with_filled_leo(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    incomplete = ORIGINAL.copy()
    incomplete[1] = None
    manager = workbook(tmp_path, [incomplete])
    gmail = Gmail()
    cmd_sync(manager, "FY 2026-27", gmail, leo_only=True, workers=1)
    assert gmail.fetched == []
    assert manager.wb.active.cell(2, 3).value == datetime(2026, 6, 6)
    assert manager.wb.active.cell(2, 9).value == "Partial"


def test_filtered_repair_does_not_stamp_unselected_cancelled_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    cancelled = ORIGINAL.copy()
    cancelled[0] = "1234567"
    cancelled[7] = "CANCELLED"
    manager = workbook(tmp_path, [ORIGINAL, cancelled])
    gmail = Gmail()
    with patch("sync.process_documents", return_value=extracted()):
        cmd_sync(manager, "FY 2026-27", gmail, force=True, rows={2}, workers=1)
    assert gmail.fetched == ["3924868"]
    assert manager.wb.active.cell(3, 9).value is None


def test_leo_only_keeps_overall_status_partial_when_other_fields_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    incomplete = ORIGINAL.copy()
    incomplete[1] = None
    incomplete[2] = None
    manager = workbook(tmp_path, [incomplete])
    gmail = Gmail()
    with patch("sync.process_documents", return_value=extracted()):
        cmd_sync(manager, "FY 2026-27", gmail, leo_only=True, workers=1)
    assert manager.wb.active.cell(2, 3).value == date(2026, 6, 8)
    assert manager.wb.active.cell(2, 2).value is None
    assert manager.wb.active.cell(2, 9).value == "Partial"


def test_forced_leo_missing_evidence_preserves_date_and_flags_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    manager = workbook(tmp_path, [ORIGINAL])
    with patch("sync.process_documents", return_value=CustomsExtractedFields()):
        cmd_sync(manager, "FY 2026-27", Gmail(), force=True, leo_only=True, workers=1)
    assert manager.wb.active.cell(2, 3).value == datetime(2026, 6, 6)
    assert manager.wb.active.cell(2, 9).value == "Partial"


def test_invalid_sheet_refuses_fallback_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    import pytest
    import typer

    manager = workbook(tmp_path, [ORIGINAL])
    before = manager.file_path.read_bytes()
    with pytest.raises(typer.BadParameter):
        cmd_sync(manager, "FY TYPO", Gmail(), force=True, workers=1)
    assert manager.file_path.read_bytes() == before
    assert manager.wb.active.cell(2, 9).value is None


def test_leo_audit_prints_evidence_without_saving(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    from models import LeoDateEvidence
    from sync import cmd_verify

    manager = workbook(tmp_path, [ORIGINAL])
    before = manager.file_path.read_bytes()
    fields = CustomsExtractedFields(leo_date="08-Jun-2026", leo_evidence=[LeoDateEvidence(
        doc_type="FINAL_LEO", page_number=1, label="6.LEO Date", raw_date="08-JUN-26",
        quote="6.LEO Date 08-JUN-26",
    )])
    with patch("sync.process_documents", return_value=fields):
        cmd_verify(manager, "FY 2026-27", Gmail(), rows={2}, leo_only=True)
    assert manager.file_path.read_bytes() == before
    assert not list(tmp_path.glob("repair.bak-*.xlsx"))
    output = capsys.readouterr().out
    assert "6.LEO Date" in output
    assert "MISMATCH" in output


def test_skipping_filled_cells_does_not_clear_a_previous_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    rejected = ORIGINAL.copy()
    rejected[8] = "Partial"
    manager = workbook(tmp_path, [rejected])
    gmail = Gmail()
    cmd_sync(manager, "FY 2026-27", gmail, leo_only=True, workers=1)
    assert gmail.fetched == []
    assert manager.wb.active.cell(2, 9).value == "Partial"
