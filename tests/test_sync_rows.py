"""Tests for the --rows spec parser. No creds, no network needed."""

import pytest
import typer

from sync import compare_audit, parse_row_spec, summarize_outcome


def test_range():
    assert parse_row_spec("1349-1351") == {1349, 1350, 1351}


def test_single_and_list():
    assert parse_row_spec("1349") == {1349}
    assert parse_row_spec("1349,1351") == {1349, 1351}
    assert parse_row_spec("1349-1350, 1351") == {1349, 1350, 1351}


def test_rejects_garbage():
    for bad in ("", "  ", "abc", "1", "0", "1351-1349", "1349-", "1349-abc"):
        with pytest.raises(typer.BadParameter):
            parse_row_spec(bad)


def test_outcome_success():
    status, _detail = summarize_outcome(
        ["leo_date", "container_no"], ["leo_date=x", "container_no=y"], docs_found=True
    )
    assert status == "Success"


def test_outcome_partial():
    status, detail = summarize_outcome(
        ["leo_date", "container_no"], ["leo_date=x"], docs_found=True
    )
    assert status == "Partial"
    assert "container_no" in detail


def test_outcome_not_found_no_docs():
    status, _ = summarize_outcome(["leo_date"], [], docs_found=False)
    assert status == "Not found"


def test_outcome_not_found_nothing_extracted():
    status, _ = summarize_outcome(["leo_date"], [], docs_found=True)
    assert status == "Not found"


def _filled(**over):
    base = {
        "shipper": "A S INTERNATIONAL",
        "invoice": "AS-101",
        "leo": "02-Sep-2026",
        "containers": "FTAU1322085",
        "sizes": "20",
        "seals": "0216467",
    }
    base.update(over)
    return base


def _got(**over):
    from models import CustomsExtractedFields

    base = {
        "shipper_name": "A S International",
        "invoice_numbers": "AS-101",
        "leo_date": "02-Sep-2026",
        "container_numbers": "FTAU1322085",
        "container_sizes": "20",
        "customs_seal_numbers": "0216467",
    }
    base.update(over)
    return CustomsExtractedFields(**base)


def test_compare_audit_all_match():
    verdicts = {f: v for f, _, _, v in compare_audit(_filled(), _got())}
    assert set(verdicts.values()) == {"match"}


def test_compare_audit_flags_mismatch_and_skips_empty():
    verdicts = {f: v for f, _, _, v in compare_audit(
        _filled(containers="", invoice="WRONG"), _got())}
    assert verdicts["containers"] == "-"
    assert verdicts["invoice"] == "MISMATCH"
    assert verdicts["shipper"] == "match"  # case-insensitive


def test_compare_audit_flags_shuffled_multi_values():
    verdicts = {f: v for f, _, _, v in compare_audit(
        _filled(containers="MSNU5724185\nMSMU8055808",
                seals="0199567\n0199568"),
        _got(container_numbers="MSMU8055808\nMSNU5724185",
             customs_seal_numbers="0199568\n0199567"))}
    assert verdicts["containers"] == "order"
    assert verdicts["seals"] == "order"
    assert verdicts["sizes"] == "match"


def test_compare_audit_date_format_equivalence():
    # Excel stores '02-Sep-2026', extraction returns '2-Sep-2026' or '2026-09-02'
    verdicts = {f: v for f, _, _, v in compare_audit(
        _filled(leo="02-Sep-2026"), _got(leo_date="2-Sep-2026"))}
    assert verdicts["leo"] == "match"

    verdicts_iso = {f: v for f, _, _, v in compare_audit(
        _filled(leo="02-Sep-2026"), _got(leo_date="2026-09-02"))}
    assert verdicts_iso["leo"] == "match"


def test_cmd_sync_stamps_cancelled(tmp_path, monkeypatch):
    import openpyxl

    from excel_manager import ExcelManager
    from sync import cmd_sync

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-test-key")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FY 2026-27"
    ws.append(["Shipping Bill No", "Inv No", "LEO Date", "Shipper Name",
               "Container No.", "Container Size", "Customs Seal Number", "Billing Status"])
    # Cancelled row
    ws.append(["2082481", None, None, None, None, None, None, "CANCELLED"])
    path = tmp_path / "sync_cancelled.xlsx"
    wb.save(path)

    mgr = ExcelManager(path)
    mgr.load()

    class DummyGmail:
        def authenticate(self):
            return True
        def fetch_documents(self, sb):
            return {}

    cmd_sync(mgr, "FY 2026-27", DummyGmail(), workers=3)

    target_ws = mgr.wb["FY 2026-27"]
    # Check Status column was added and row 2 stamped 'Cancelled'
    status_col = mgr.ensure_status_column("FY 2026-27")
    assert target_ws.cell(row=2, column=status_col).value == "Cancelled"


def test_process_documents_empty():
    from models import CustomsExtractedFields
    from sync import process_documents

    assert process_documents({}) == CustomsExtractedFields()


def test_process_documents_unified(monkeypatch):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    mock_trim = MagicMock(return_value=("Sample PDF Text with 1234567", [0]))
    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_date="10-Sep-2026",
            container_numbers="TCKU1234567",
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        docs = {
            "eGatepass": ("gatepass.pdf", b"pdfbytes1"),
            "finalLeo": ("leo.pdf", b"pdfbytes2"),
        }
        res = process_documents(docs, sb_no="1234567")
        assert res.shipper_name == "ACME EXPORTS"
        assert res.invoice_numbers == "INV/001"
        assert res.container_numbers == "TCKU1234567"

        # Verify extract_with_ai was called once with doc_type="COMBINED"
        assert mock_extract.call_count == 1
        call_args = mock_extract.call_args
        prompt_text = call_args[0][0]
        assert "=== DOCUMENT: Customs Gatepass (SB35) (gatepass.pdf) ===" in prompt_text
        assert "=== DOCUMENT: Final LEO Shipping Bill (SB22) (leo.pdf) ===" in prompt_text
        assert call_args[1]["doc_type"] == "COMBINED"


def test_cmd_sync_with_sb_filter(tmp_path, monkeypatch):
    import openpyxl

    from excel_manager import ExcelManager
    from models import CustomsExtractedFields
    from sync import cmd_sync

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FY 2026-27"
    ws.append(["Shipping Bill No", "Inv No", "LEO Date", "Shipper Name",
               "Container No.", "Container Size", "Customs Seal Number", "Billing Status"])
    # Row 2: SB 1278254 (target)
    ws.append(["1278254", None, None, None, None, None, None, None])
    # Row 3: SB 8743735 (other missing SB)
    ws.append(["8743735", None, None, None, None, None, None, None])
    path = tmp_path / "sync_sb_filter.xlsx"
    wb.save(path)

    mgr = ExcelManager(path)
    mgr.load()

    fetched_sbs = []

    class DummyGmail:
        def authenticate(self):
            return True
        def fetch_documents(self, sb):
            fetched_sbs.append(sb)
            return {"eGatepass": ("gp.pdf", b"dummy")}

    mock_extract = CustomsExtractedFields(
        shipper_name="TARGET SHIPPER",
        leo_date="10-Sep-2026",
    )

    from unittest.mock import patch
    with patch("sync.process_documents", return_value=mock_extract):
        cmd_sync(mgr, "FY 2026-27", DummyGmail(), workers=1, sb_filter="1278254")

    # Only 1278254 should have been fetched and updated
    assert fetched_sbs == ["1278254"]
    target_ws = mgr.wb["FY 2026-27"]
    assert target_ws.cell(row=2, column=4).value == "TARGET SHIPPER"
    # Row 3 should remain empty
    assert target_ws.cell(row=3, column=4).value is None


