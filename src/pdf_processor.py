import re

import anydoc
import pymupdf


class PdfConversionError(RuntimeError):
    """A kept PDF page could not be converted to native Markdown."""


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
    Uses unsorted PyMuPDF native text only to select relevant pages, then converts
    each selected original page independently through AnyDoc. The returned text
    includes one-based source page markers and kept_page_indices remains zero-based.
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

        for i, page in enumerate(doc):
            page_text = page.get_text()
            if is_page_relevant(i, page_text, doc_type):
                kept_indices.append(i)

        if not kept_indices:
            kept_indices = [0]

        text_chunks: list[str] = []
        for page_index in kept_indices:
            selected_page = pymupdf.open()
            try:
                selected_page.insert_pdf(doc, from_page=page_index, to_page=page_index)
                page_bytes = selected_page.tobytes()
            finally:
                selected_page.close()

            try:
                markdown = anydoc.to_markdown_bytes(page_bytes, "pdf", ocr="reject")
            except anydoc.NeedsOcrError as exc:
                raise PdfConversionError(
                    f"PDF page {page_index + 1} requires OCR; OCR is disabled for native AnyDoc conversion."
                ) from exc
            except anydoc.ConvertError as exc:
                raise PdfConversionError(
                    f"AnyDoc could not convert PDF page {page_index + 1} to Markdown: {exc}"
                ) from exc
            if not markdown.strip():
                raise PdfConversionError(f"AnyDoc returned no Markdown for PDF page {page_index + 1}.")

            text_chunks.append(f"--- SOURCE PAGE {page_index + 1} ---\n{markdown.strip()}")

        combined_text = "\n\n".join(text_chunks)
        return combined_text, kept_indices
    except PdfConversionError:
        raise
    except Exception:
        return "", []
    finally:
        doc.close()


_SOURCE_PAGE_RE = re.compile(r"(?m)^--- SOURCE PAGE (\d+) ---\s*$")


def split_source_pages(markdown: str) -> dict[int, str]:
    """Returns converted Markdown by original one-based page number."""
    matches = list(_SOURCE_PAGE_RE.finditer(markdown))
    if not matches:
        return {1: markdown} if markdown.strip() else {}

    pages: dict[int, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        pages[int(match.group(1))] = markdown[start:end].strip()
    return pages


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
