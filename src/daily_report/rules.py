"""ICEGATE policy. No service or filesystem operations belong here."""

from __future__ import annotations

import re
from datetime import datetime

from daily_report.models import BillIdentity, DocumentMetadata

SENDER = "noreply@icegate.gov.in"
RULES_VERSION = "4"
PARSER_VERSION = "4"
LAYOUT_VERSION = "2"
FIELD_DOCUMENTS = {
    "shipper_name": "SB21",
    "inv_no": "SB21",
    "leo_date": "SB22",
    "container_no": "SB35",
    "container_size": "SB35",
    "customs_seal": "SB35",
}
DATA_FIELDS = tuple(FIELD_DOCUMENTS)
PAGES = {"SB21": (1, 2), "SB22": (1,), "SB35": None}
EXCEL_COLUMNS = {
    "sl_no": ["Sl No", "S.No", "Sl.No"],
    "fy": ["Financial Year", "FY"],
    "port_code": ["Port Code / ICD Code", "Port Code", "ICD Code"],
    "job_no": ["Job No", "Job No."],
    "job_date": ["Job Date", "Job Dt"],
    "inv_no": ["Inv No", "Invoice No", "Invoice Number", "Inv No.", "Invoice No."],
    "sb_no": ["Shipping Bill No", "Shipping Bill Number", "SB No", "SB Number"],
    "sb_date": ["Shipping Bill Date", "SB Date", "Shipping Bill Dt", "SB Dt"],
    "document_date": ["Document Date", "Document Dt"],
    "leo_date": ["LEO Date", "LEO Dt", "LEO Date.", "Let Export Date"],
    "shipper_name": ["Shipper Name", "Exporter Name", "Shipper", "Exporter", "Party Name"],
    "container_no": ["Container No.", "Container No", "Container Number", "Container", "Containers"],
    "container_size": ["Container Size", "Size", "Cont Size", "Cntr Size"],
    "customs_seal": [
        "Customs Seal Number",
        "Customs Seal No",
        "Customs Seal No.",
        "Customs Seal",
        "Seal Number",
        "Seal No",
    ],
    "remarks": ["Remarks", "Remarks / Status", "Sync Status"],
    "billing_status": ["Billing Status", "Billing status", "Billing"],
    "status": ["Status"],
}
EXCEL_REQUIRED_COLUMNS = ("sb_no", *DATA_FIELDS)
EXCEL_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d%m%Y", "%d-%b-%y")
EXCEL_DATE_NUMBER_FORMAT = "DD/MM/YYYY"
EXCEL_BLANK_VALUES = ("", "-", "N/A", "NA", "NONE", "NULL")
EXCEL_CANCELLED_VALUE = "CANCELLED"
LABELS = {
    "shipper_name": r"1\s*\.\s*EXPORTER['\u2019]?S\s+NAME\s*&\s*ADDRESS",
    "inv_no": r"2\s*\.\s*INVOICE\s+No\.?\s*&\s*Dt\.?",
    "leo_date": r"6\s*\.\s*LEO\s+Date\.?",
    "container_no": r"11\s*[.,]\s*Container(?:\s+No\.?)?",
    "customs_seal": r"12\s*\.\s*Seal(?:\s+No\.?)?",
    "container_size": r"14\s*\.\s*Size",
    "sb_no": r"(?:\d+\s*\.\s*)?(?:SB\s*No\.?|Shipping\s+Bill\s+No\.?)",
    "sb_date": r"(?:\d+\s*\.\s*)?(?:SB\s*(?:Date|DT)|Shipping\s+Bill\s+Date)",
    "port_code": r"(?:\d+\s*\.\s*)?Port\s+Code",
}
NUMBERED_LABEL = re.compile(r"^\d+\s*\.\s*[A-Za-z]")
ROW_TOLERANCE = 3.0
COLUMN_TOLERANCE = 3.0
MAX_CELL_DEPTH = 65.0
MAX_INLINE_GAP = 25.0
CONTAINER_FIELDS = ("container_no", "customs_seal", "container_size")
CONTAINER_ATTRS = {"container_no": "number", "container_size": "size", "customs_seal": "seal"}
LEO_FIELDS = frozenset({"leo_date"})
MOVEMENT_STOP = re.compile(r"^(?:\d+\s*\.\s*[A-Za-z]|DECLARATION|NOTE\b|SIGNATURE|PAGE\s+\d)", re.I)


def parse_filename(filename: str) -> DocumentMetadata:
    if not re.fullmatch(r"\d{15}[A-Z0-9]{6}(?:SB21|SB22|SB35)\d{12}\.pdf", filename):
        raise ValueError("Filename is not canonical ICEGATE metadata")
    try:
        sb_date = datetime.strptime(filename[7:15], "%d%m%Y").date()
        document_at = datetime.strptime(filename[25:37], "%d%m%Y%H%M")
    except ValueError as exc:
        raise ValueError("Invalid filename date or time") from exc
    return DocumentMetadata(
        identity=BillIdentity(
            sb_no=filename[:7], sb_date=sb_date, port_code=filename[15:21], document_date=document_at.date()
        ),
        code=filename[21:25],
        document_at=document_at,
        filename=filename,
    )


def filename_prefix(identity: BillIdentity, code: str) -> str:
    if code not in PAGES:
        raise ValueError("Unsupported document code")
    prefix = identity.sb_no
    if identity.sb_date is None:
        return prefix
    prefix += identity.sb_date.strftime("%d%m%Y")
    if identity.port_code is None:
        return prefix
    prefix += identity.port_code + code
    if identity.document_date:
        prefix += identity.document_date.strftime("%d%m%Y")
    return prefix


def gmail_identity_terms(identity: BillIdentity) -> tuple[str, ...]:
    """Exact indexed identity tokens; Gmail doesn't search filename prefixes."""
    if identity.sb_date is None:
        return ()
    return (identity.sb_no, f"No{identity.sb_no}Dt{identity.sb_date:%d%m%Y}")


def matches_identity(expected: BillIdentity, actual: BillIdentity) -> bool:
    return all(
        getattr(expected, key) is None or getattr(expected, key) == getattr(actual, key)
        for key in ("sb_no", "sb_date", "port_code", "document_date")
    )


def document_codes(fields: set[str]) -> set[str]:
    return {FIELD_DOCUMENTS[field] for field in fields if field in FIELD_DOCUMENTS}


def discovery_codes(fields: set[str]) -> set[str]:
    return document_codes(fields) if fields == LEO_FIELDS else set(FIELD_DOCUMENTS.values())


def label_matches(field: str, text: str) -> bool:
    return bool(re.fullmatch(LABELS[field], text.strip(), re.I))


def value_token(field: str, text: str):
    """Validate only a geometrically associated cell, never search a page."""
    text = text.strip()
    if text.upper() in EXCEL_BLANK_VALUES:
        return None
    if field in ("leo_date", "sb_date"):
        for fmt in EXCEL_DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass
        raise ValueError("Invalid associated date")
    if field == "sb_no" and not re.fullmatch(r"\d{7}", text):
        raise ValueError("Invalid associated SB number")
    if field == "port_code" and not re.fullmatch(r"[A-Z0-9]{6}", text):
        raise ValueError("Invalid associated port")
    if field == "container_no" and not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", text):
        raise ValueError("Invalid movement container number")
    if field == "container_size" and not re.fullmatch(r"(?:20|40|45)(?:\s*(?:FT|HC|GP|HQ))?", text, re.I):
        raise ValueError("Invalid movement container size")
    if field == "customs_seal" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]*", text):
        raise ValueError("Invalid movement seal")
    if field == "shipper_name":
        if not EXPORTER_NAME.fullmatch(text) or ADDRESS_ONLY.search(text):
            raise ValueError("Exporter cell contains address-only or unsupported company evidence")
    if field == "inv_no":
        for fmt in EXCEL_DATE_FORMATS:
            try:
                datetime.strptime(text.split()[0], fmt)
            except ValueError:
                continue
            raise ValueError("Invoice cell contains date without primary invoice token")
        parts = text.split()
        token = parts[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]*", token):
            raise ValueError("Invalid primary invoice token")
        if len(parts) > 1:
            # Remaining cell contents must start with the associated invoice date.
            value_token("sb_date", parts[1])
        return token
    return text


EXCEL_DATE_FIELDS = frozenset({"sb_date", "document_date", "leo_date"})
SB_NUMBER_LENGTH = 7
CELL_LEFT_MARGIN = {"sb_no": 12.0, "sb_date": 12.0, "port_code": 12.0, "inv_no": 40.0}
INLINE_VALUE_FIELDS = frozenset({"leo_date", "sb_no", "sb_date", "port_code", *CONTAINER_FIELDS})
SOURCE_PAGE_PRIORITY = {"shipper_name": (1, 2)}

NUMBERED_PREFIX = re.compile(r"^\d+\s*[.,]$")
ADDRESS_ONLY = re.compile(
    r"^(?:\d+\b|(?:P\.?\s*O\.?\s*BOX|ADDRESS|ROAD|STREET|PLOT|HOUSE|FLOOR|SECTOR|VILLAGE|DISTRICT|PIN\s*CODE)\b)|\b(?:ADDRESS\s+ROAD|ROAD|STREET|LANE|POST\s+OFFICE|PIN\s*CODE)\s*$",
    re.I,
)
EXPORTER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 &'().,/-]*$")
IDENTITY_FIELDS = ("sb_no", "sb_date", "port_code")

SECONDARY_IDENTITY = {
    "anchor": r"1\s*\.\s*SB\s*NO",
    "companion": r"4\s*\.\s*SB\s*DT",
    "field": "sb_date",
    "row_offset": 13.5,
}


def secondary_identity_label(text: str, role: str) -> bool:
    return bool(re.fullmatch(SECONDARY_IDENTITY[role], text.strip(), re.I))
