import re

import pymupdf


def classify_document(filename: str, text: str = "") -> str:
    """Classifies document slot: 'eGatepass', 'finalLeo', 'firstCopy', or 'UNKNOWN'."""
    combined_upper = (filename + " " + text).upper()
    code_match = re.search(r"SB[-_\s]?(21|22|35)(?!\d)", combined_upper)
    if code_match:
        code = code_match.group(1)
        if code == "35":
            return "eGatepass"
        if code == "22":
            return "finalLeo"
        return "firstCopy"

    low_text = (filename + " " + text).lower()
    if re.search(r"e-?gatepass|gate\s*pass", low_text):
        return "eGatepass"
    if re.search(r"final\s*leo|\bleo\s*copy\b", low_text):
        return "finalLeo"
    if re.search(r"first\s*copy", low_text):
        return "firstCopy"
    return "UNKNOWN"


def is_page_relevant(page_num: int, text: str, doc_type: str) -> bool:
    """
    Determines if a PDF page contains critical customs fields or is redundant.
    Aggressively discards item catalogs (Part III), drawback claims (Part IV),
    terms/conditions, and declaration pages.
    """
    if doc_type == "EGATEPASS":
        # Keep pages with movement rows, CHA exporter, order date, or container numbers
        return bool(re.search(r"(Allowed\s+for\s+Movement|4\.\s*CHA\b|1\.\s*Order\s+No\s*/\s*date|\b[A-Z]{3}[UJZ]\d{7}\b)", text, re.IGNORECASE))

    # Shipping bills (FIRST_COPY, FINAL_LEO)
    if page_num == 0:
        return True

    # Immediate rejection of product catalogs, drawback claims, and declarations
    if re.search(r"(PART\s*-\s*III\b|PART\s*-\s*IV\b|ANNEXURE|DECLARATION\s*BY)", text, re.IGNORECASE):
        if not re.search(r"(?:H\.\s*)?INVOICE\s+DETAILS|2\.\s*INV\s*NO", text, re.IGNORECASE):
            return False

    # Keep only pages containing invoice details, LEO date, 9.LEO event, or container details
    return bool(re.search(r"((?:H\.\s*)?INVOICE\s+DETAILS|2\.\s*INV\s*NO|6\.\s*LEO\s*D(?:a)?t(?:e)?|9\.\s*LEO\b|CONTAINER\s+DETAILS)", text, re.IGNORECASE))


def trim_pdf(
    pdf_input: bytes | str,
    doc_type: str = "UNKNOWN",
) -> tuple[str, list[int]]:
    """
    Fast in-memory scanning of PDF pages using PyMuPDF.
    Discards redundant pages, slashes token size by 80-90%,
    and returns (combined_text, kept_page_indices).
    Properly closes the document to prevent native memory leaks.
    """
    if not pdf_input:
        return "", []

    try:
        if isinstance(pdf_input, (bytes, bytearray)):
            doc = pymupdf.open(stream=pdf_input, filetype="pdf")
        else:
            doc = pymupdf.open(pdf_input)
    except Exception:
        return "", []

    try:
        if doc.is_encrypted and not doc.authenticate(""):
            return "", []

        if len(doc) == 0:
            return "", []

        kept_indices: list[int] = []
        text_chunks: list[str] = []

        for i, page in enumerate(doc):
            page_text = page.get_text()
            if is_page_relevant(i, page_text, doc_type):
                kept_indices.append(i)
                text_chunks.append(page_text)

        if not kept_indices:
            kept_indices = [0]
            text_chunks = [doc[0].get_text()]

        combined_text = "\n\n".join(text_chunks)
        return combined_text, kept_indices
    except Exception:
        return "", []
    finally:
        doc.close()


def verify_identity(text: str, expected_sb_no: str) -> bool:
    """Verifies that the PDF text matches the requested Shipping Bill number."""
    if not expected_sb_no:
        return True
    sb_str = str(expected_sb_no).strip()
    if sb_str.endswith(".0"):
        sb_str = sb_str[:-2].strip()
    pattern = rf"(?:SB\s*No|SBNO|Shipping\s*Bill\s*No)[\s\S]{{0,60}}?\b{re.escape(sb_str)}\b"
    if re.search(pattern, text, re.IGNORECASE):
        return True
    return bool(re.search(rf"\b{re.escape(sb_str)}\b", text))
