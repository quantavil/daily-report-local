import pymupdf

from pdf_processor import classify_document, is_page_relevant, trim_pdf, verify_identity


def test_classify_document():
    assert classify_document("1278254_SB35_gatepass.pdf") == "eGatepass"
    assert classify_document("1278254_SB22_final.pdf") == "finalLeo"
    assert classify_document("1278254_SB21_first.pdf") == "firstCopy"
    assert classify_document("doc.pdf", text="Electronic Gate Pass Copy") == "eGatepass"
    assert classify_document("doc.pdf", text="Final LEO Copy") == "finalLeo"
    assert classify_document("doc.pdf", text="First Copy") == "firstCopy"
    assert classify_document("doc.pdf", text="Random text") == "UNKNOWN"


def test_is_page_relevant_egatepass():
    assert is_page_relevant(0, "Allowed for Movement table", "EGATEPASS") is True
    assert is_page_relevant(0, "4.CHA details", "EGATEPASS") is True
    assert is_page_relevant(0, "4. CHA details with space", "EGATEPASS") is True
    assert is_page_relevant(0, "1. Order No / date 28-Apr-2025", "EGATEPASS") is True
    assert is_page_relevant(0, "Just random terms and conditions", "EGATEPASS") is False


def test_is_page_relevant_shipping_bill():
    # Page 0 is always relevant
    assert is_page_relevant(0, "Part I header", "FINAL_LEO") is True

    # Page 1+ discard item catalogs
    assert is_page_relevant(1, "PART - III ITEM DETAILS catalog item 1", "FINAL_LEO") is False
    assert is_page_relevant(1, "PART IV DRAWBACK PARTICULARS", "FINAL_LEO") is False

    # Page 1+ keep invoice details or containers or LEO
    assert is_page_relevant(1, "H.INVOICE DETAILS Inv No 101", "FINAL_LEO") is True
    assert is_page_relevant(1, "H. INVOICE DETAILS Inv No 101", "FINAL_LEO") is True
    assert is_page_relevant(1, "6. LEO Date 10-Sep-2026", "FINAL_LEO") is True
    assert is_page_relevant(1, "9. LEO 06-Apr-2024 Event Grant", "FINAL_LEO") is True
    assert is_page_relevant(1, "CONTAINER DETAILS TCKU1234567", "FINAL_LEO") is True


def test_trim_pdf_empty_bytes():
    text, kept = trim_pdf(b"", "EGATEPASS")
    assert text == ""
    assert kept == []


def test_trim_pdf_single_page():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "SB No: 1278254 Allowed for Movement TCKU1234567")
    pdf_bytes = doc.tobytes()
    doc.close()

    text, kept = trim_pdf(pdf_bytes, "EGATEPASS")
    assert kept == [0]
    assert "Allowed for Movement" in text


def test_verify_identity():
    assert verify_identity("Shipping Bill No. 1278254 Date: 28-Apr-2025", "1278254") is True
    assert verify_identity("SBNO: 1278254", "1278254") is True
    assert verify_identity("Some other doc 9999999", "1278254") is False
    assert verify_identity("Any text when sb is empty", "") is True
