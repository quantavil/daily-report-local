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
        def fetch_documents(self, sb, *, fields=None, include_first_copy=False):
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

    from models import CustomsExtractedFields, LeoDateEvidence
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    def mock_trim(pdf_bytes, doc_type, *, leo_only=False):
        if doc_type == "EGATEPASS":
            return ("Shipping Bill No: 1234567\n1.Order No / date 1/101 10-Sep-2026", [0])
        return (
            "Shipping Bill No: 1234567\n"
            "5.Submission 06-Sep-2026 6.LEO Date 10-Sep-2026\n9.LEO 10-Sep-2026",
            [0],
        )

    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_date="01-Jan-2000",
            leo_evidence=[
                LeoDateEvidence(
                    doc_type="EGATEPASS",
                    page_number=1,
                    label="1.Order No / date",
                    raw_date="10-Sep-2026",
                    quote="1.Order No / date 1/101 10-Sep-2026",
                ),
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="6.LEO Date",
                    raw_date="10-Sep-2026",
                    quote="5.Submission 06-Sep-2026 6.LEO Date 10-Sep-2026",
                ),
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="9.LEO",
                    raw_date="10-Sep-2026",
                    quote="9.LEO 10-Sep-2026",
                ),
            ],
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
        assert res.leo_date == "10-Sep-2026"
        assert len(res.leo_evidence) == 3
        assert res.container_numbers == "TCKU1234567"

        # Verify extract_with_ai was called once with doc_type="COMBINED"
        assert mock_extract.call_count == 1
        call_args = mock_extract.call_args
        prompt_text = call_args[0][0]
        assert "=== DOCUMENT: Customs Gatepass (SB35) (gatepass.pdf) ===" in prompt_text
        assert "=== DOCUMENT: Final LEO Shipping Bill (SB22) (leo.pdf) ===" in prompt_text
        assert call_args[1]["doc_type"] == "COMBINED"


def test_process_documents_first_copy_only_clears_leo_date(monkeypatch):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    mock_trim = MagicMock(return_value=("Sample First Copy text for SB 7602292", [0]))
    # Mock AI returning hallucinated LEO date and container fields from SB21
    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_date="09-Oct-2026",
            container_numbers="HALLUCINATED_CONTAINER",
            container_sizes="20",
            customs_seal_numbers="SEAL123",
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        docs = {
            "firstCopy": ("sb21.pdf", b"pdfbytes_sb21"),
        }
        res = process_documents(docs, sb_no="7602292")
        assert res.shipper_name == "ACME EXPORTS"
        assert res.invoice_numbers == "INV/001"
        assert res.leo_date is None
        assert res.container_numbers is None
        assert res.container_sizes is None
        assert res.customs_seal_numbers is None


def test_process_documents_final_leo_keeps_leo_date_clears_containers(monkeypatch):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields, LeoDateEvidence
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    mock_trim = MagicMock(
        return_value=(
            "Shipping Bill No: 7071911\n5.Submission 07-Oct-2026 6.LEO Date 08-Oct-2026\n"
            "9.LEO 08-Oct-2026",
            [0],
        )
    )
    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_date="01-Jan-2000",
            leo_evidence=[
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="6.LEO Date",
                    raw_date="08-Oct-2026",
                    quote="5.Submission 07-Oct-2026 6.LEO Date 08-Oct-2026",
                ),
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="9.LEO",
                    raw_date="08-Oct-2026",
                    quote="9.LEO 08-Oct-2026",
                ),
            ],
            container_numbers="HALLUCINATED_CONTAINER",
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        docs = {
            "finalLeo": ("sb22.pdf", b"pdfbytes_sb22"),
        }
        res = process_documents(docs, sb_no="7071911")
        assert res.shipper_name == "ACME EXPORTS"
        assert res.leo_date == "08-Oct-2026"
        assert res.container_numbers is None


def test_process_documents_does_not_trust_unverified_ai_leo_date_and_keeps_other_fields(monkeypatch):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    mock_trim = MagicMock(
        return_value=(
            "Shipping Bill No: 1234567\n"
            "**5.Submission** 06-JUN-26 15:32 **6.LEO Date.** 08-JUN-26",
            [0],
        )
    )
    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_date="06-Jun-2026",
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        result = process_documents({"finalLeo": ("sb22.pdf", b"pdfbytes")}, sb_no="1234567")

    assert result.shipper_name == "ACME EXPORTS"
    assert result.invoice_numbers == "INV/001"
    assert result.leo_date is None
    assert result.leo_warning


def test_process_documents_conflict_refuses_leo_and_keeps_other_fields(monkeypatch, capsys):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields, LeoDateEvidence
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    gatepass_text = "Shipping Bill No: 1234567\n1.Order No / date 1/101 08-JUN-26"
    final_text = (
        "Shipping Bill No: 1234567\n"
        "5.Submission 06-JUN-26 6.LEO Date 09-JUN-26\n9.LEO 09-JUN-26"
    )

    def mock_trim(_pdf_bytes, doc_type, *, leo_only=False):
        return (gatepass_text if doc_type == "EGATEPASS" else final_text, [0])

    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="INV/001",
            leo_evidence=[
                LeoDateEvidence(
                    doc_type="EGATEPASS",
                    page_number=1,
                    label="1.Order No / date",
                    raw_date="08-JUN-26",
                    quote="1.Order No / date 1/101 08-JUN-26",
                ),
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="6.LEO Date",
                    raw_date="09-JUN-26",
                    quote="5.Submission 06-JUN-26 6.LEO Date 09-JUN-26",
                ),
                LeoDateEvidence(
                    doc_type="FINAL_LEO",
                    page_number=1,
                    label="9.LEO",
                    raw_date="09-JUN-26",
                    quote="9.LEO 09-JUN-26",
                ),
            ],
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        result = process_documents(
            {
                "eGatepass": ("sb35.pdf", b"gatepass"),
                "finalLeo": ("sb22.pdf", b"final"),
            },
            sb_no="1234567",
        )

    assert result.leo_date is None
    assert "conflicting" in result.leo_warning.lower()
    assert result.shipper_name == "ACME EXPORTS"
    assert result.invoice_numbers == "INV/001"
    assert "LEO date not applied" in capsys.readouterr().out



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
        def fetch_documents(self, sb, *, fields=None, include_first_copy=False):
            fetched_sbs.append(sb)
            return {"eGatepass": ("gp.pdf", b"dummy")}

    mock_extract = CustomsExtractedFields(
        invoice_numbers="TARGET-INV",
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


def test_models_clean_placeholders_and_formatting():
    from models import CustomsExtractedFields

    fields = CustomsExtractedFields(
        shipper_name="  \"ACME EXPORTS LTD.\" , ",
        invoice_numbers=["NF/156/2025-26 Dt. 28/04/2025", "INV/002"],
        leo_date="null",
        container_numbers="TCKU 7809338\nUNSU-0088111",
        container_sizes="40 FT\n20",
        customs_seal_numbers=["SEAL NO: 012345", "Pkg", "SEAL123"],
    )
    assert fields.shipper_name == "ACME EXPORTS LTD."
    assert fields.invoice_numbers == "NF/156/2025-26"
    assert fields.leo_date is None
    assert fields.container_numbers == "TCKU7809338\nUNSU0088111"
    assert fields.container_sizes == "40\n20"
    assert fields.customs_seal_numbers == "012345\nSEAL123"


def test_process_documents_gatepass_only_clears_invoice_numbers(monkeypatch):
    from unittest.mock import MagicMock, patch

    from models import CustomsExtractedFields, LeoDateEvidence
    from sync import process_documents

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-mock-key")

    mock_trim = MagicMock(
        return_value=(
            "Shipping Bill No: 1278254\n1.Order No / date 1/101 28-Apr-2025",
            [0],
        )
    )
    # Mock AI hallucinating an invoice number from a gatepass
    mock_extract = MagicMock(
        return_value=CustomsExtractedFields(
            shipper_name="ACME EXPORTS",
            invoice_numbers="HALLUCINATED_INV",
            leo_date="01-Jan-2000",
            leo_evidence=[
                LeoDateEvidence(
                    doc_type="EGATEPASS",
                    page_number=1,
                    label="1.Order No / date",
                    raw_date="28-Apr-2025",
                    quote="1.Order No / date 1/101 28-Apr-2025",
                )
            ],
            container_numbers="TCKU7809338",
            container_sizes="40",
            customs_seal_numbers="012345",
        )
    )

    with patch("sync.trim_pdf", mock_trim), patch("sync.extract_with_ai", mock_extract):
        docs = {
            "eGatepass": ("sb35.pdf", b"pdfbytes_sb35"),
        }
        res = process_documents(docs, sb_no="1278254")
        assert res.shipper_name == "ACME EXPORTS"
        assert res.leo_date == "28-Apr-2025"
        assert res.container_numbers == "TCKU7809338"
        # Invariant: Gatepass has no invoice numbers, must be cleared to None
        assert res.invoice_numbers is None


def test_cmd_sync_with_sb_filter_force_on_complete_row(tmp_path, monkeypatch):
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
    # Row 2: SB 1278254 already complete
    ws.append(["1278254", "OLD-INV", "28-Apr-2025", "OLD SHIPPER", "OLDCONT", "20", "OLDSEAL", None])
    path = tmp_path / "sync_force_complete.xlsx"
    wb.save(path)

    mgr = ExcelManager(path)
    mgr.load()

    fetched_sbs = []

    class DummyGmail:
        def authenticate(self):
            return True
        def fetch_documents(self, sb, *, fields=None, include_first_copy=False):
            fetched_sbs.append(sb)
            return {"eGatepass": ("gp.pdf", b"dummy")}

    mock_extract = CustomsExtractedFields(
        invoice_numbers="FORCED-INV",
        shipper_name="FORCED SHIPPER",
        leo_date="10-Sep-2026",
    )

    from unittest.mock import patch
    with patch("sync.process_documents", return_value=mock_extract):
        # With force=True, complete row should be re-fetched and overwritten
        cmd_sync(mgr, "FY 2026-27", DummyGmail(), workers=1, sb_filter="1278254", force=True)

    assert fetched_sbs == ["1278254"]
    target_ws = mgr.wb["FY 2026-27"]
    assert target_ws.cell(row=2, column=4).value == "FORCED SHIPPER"


def test_gmail_client_fetch_documents_unpadded_base64():
    from unittest.mock import MagicMock

    from gmail_client import GmailClient

    client = GmailClient()
    mock_service = MagicMock()
    client.service = mock_service

    # Mock list returning 1 message
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg1"}]
    }

    # Mock get returning message payload with attachment
    mock_service.users().messages().get().execute.return_value = {
        "payload": {
            "headers": [{"name": "Subject", "value": "Electronic eGatepass copy of SB for Shipping Bill 1278254"}],
            "parts": [
                {
                    "filename": "1278254_SB35.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att123"},
                }
            ],
        }
    }

    # Unpadded base64 data ('abc' is length 3, raises error if padding not handled)
    mock_service.users().messages().attachments().get().execute.return_value = {
        "data": "abc"
    }

    docs = client.fetch_documents("1278254")
    assert "eGatepass" in docs
    assert docs["eGatepass"][0] == "1278254_SB35.pdf"


def test_cmd_verify_cleans_sb_number(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-unit-test-key")
    from unittest.mock import patch

    import openpyxl

    from excel_manager import ExcelManager
    from models import CustomsExtractedFields
    from sync import cmd_verify

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FY 2026-27"
    ws.append(["Shipping Bill No", "Inv No", "LEO Date", "Shipper Name",
               "Container No.", "Container Size", "Customs Seal Number"])
    # Row 2: SB number formatted as float or with whitespace
    ws.append(["  1278254  ", "INV1", "28-Apr-2025", "SHIPPER", "CONT1", "20", "SEAL1"])
    path = tmp_path / "verify_clean.xlsx"
    wb.save(path)

    mgr = ExcelManager(path)
    mgr.load()

    queried_sbs = []

    class MockGmail:
        def authenticate(self):
            return True
        def fetch_documents(self, sb_no, *, fields=None, include_first_copy=False):
            queried_sbs.append(sb_no)
            return {"eGatepass": ("gp.pdf", b"pdfbytes")}

    mock_extract = CustomsExtractedFields(
        invoice_numbers="INV1",
        shipper_name="SHIPPER",
        leo_date="28-Apr-2025",
    )

    with patch("sync.process_documents", return_value=mock_extract):
        cmd_verify(mgr, "FY 2026-27", MockGmail(), rows={2})

    assert queried_sbs == ["1278254"]


def test_models_preserves_duplicate_container_sizes_and_seals():
    from models import CustomsExtractedFields

    fields = CustomsExtractedFields(
        container_numbers="TCKU7809338\nUNSU0088111",
        container_sizes="40 FT\n40 FT",
        customs_seal_numbers=["SEAL01", "SEAL01"],
    )
    # Container sizes and seals must NOT collapse across containers
    assert fields.container_sizes == "40\n40"
    assert fields.customs_seal_numbers == "SEAL01\nSEAL01"


def test_models_handles_empty_list_and_non_string_leo_date():
    from models import CustomsExtractedFields

    # Empty list should not raise ValidationError
    fields = CustomsExtractedFields(
        leo_date=[],
        shipper_name=[],
        invoice_numbers=[],
    )
    assert fields.leo_date is None
    assert fields.shipper_name is None
    assert fields.invoice_numbers is None

    # Single-item list for leo_date
    fields2 = CustomsExtractedFields(leo_date=["28-Apr-2025"])
    assert fields2.leo_date == "28-Apr-2025"


def test_models_clean_invoices_strips_trailing_hyphen():
    from models import CustomsExtractedFields

    fields = CustomsExtractedFields(
        invoice_numbers="INV-101 - Dt. 28/04/2025",
    )
    assert fields.invoice_numbers == "INV-101"


def test_gmail_client_inline_attachment():
    import base64
    from unittest.mock import MagicMock

    from gmail_client import GmailClient

    client = GmailClient()
    mock_service = MagicMock()
    client.service = mock_service

    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg_inline"}]
    }

    dummy_pdf_data = base64.urlsafe_b64encode(b"%PDF-1.4 dummy").decode("ascii")
    # Inline attachment: data directly in body, no attachmentId
    mock_service.users().messages().get().execute.return_value = {
        "payload": {
            "headers": [{"name": "Subject", "value": "Electronic eGatepass copy of SB for Shipping Bill 1278254"}],
            "parts": [
                {
                    "filename": "1278254_SB35.pdf",
                    "mimeType": "application/pdf",
                    "body": {"data": dummy_pdf_data},
                }
            ],
        }
    }

    docs = client.fetch_documents("1278254")
    assert "eGatepass" in docs
    assert docs["eGatepass"][1] == b"%PDF-1.4 dummy"


def test_cmd_sync_with_rows_filter_force_on_complete_row(tmp_path, monkeypatch):
    from unittest.mock import patch

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
    # Row 2 is already complete
    ws.append(["1278254", "OLD-INV", "28-Apr-2025", "OLD SHIPPER", "OLDCONT", "20", "OLDSEAL", None])
    path = tmp_path / "sync_rows_force.xlsx"
    wb.save(path)

    mgr = ExcelManager(path)
    mgr.load()

    queried_sbs = []

    class DummyGmail:
        def authenticate(self):
            return True
        def fetch_documents(self, sb, *, fields=None, include_first_copy=False):
            queried_sbs.append(sb)
            return {"eGatepass": ("gp.pdf", b"dummy")}

    mock_extract = CustomsExtractedFields(
        invoice_numbers="ROWS-INV",
        shipper_name="FORCED BY ROWS",
        leo_date="10-Sep-2026",
    )

    with patch("sync.process_documents", return_value=mock_extract):
        cmd_sync(mgr, "FY 2026-27", DummyGmail(), workers=1, rows={2}, force=True)

    assert queried_sbs == ["1278254"]
    target_ws = mgr.wb["FY 2026-27"]
    assert target_ws.cell(row=2, column=4).value == "FORCED BY ROWS"

