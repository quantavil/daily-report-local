import re
from datetime import date

from pydantic import BaseModel, Field, field_validator


class CustomsExtractedFields(BaseModel):
    """Structured customs data model for Pydantic v2 validation and OpenRouter output."""
    shipper_name: str | None = Field(
        default=None,
        description="Exporter / Shipper company or entity name strictly without address details."
    )
    invoice_numbers: str | None = Field(
        default=None,
        description="Invoice number(s) from Section H / INVOICE DETAILS."
    )
    leo_date: str | None = Field(
        default=None,
        description="LEO Date or Gatepass Order Date formatted as DD-Mon-YYYY (e.g. 28-Apr-2025)."
    )
    container_numbers: str | None = Field(
        default=None,
        description="ISO container numbers (e.g. UNSU0088111, TCKU7809338)."
    )
    container_sizes: str | None = Field(
        default=None,
        description="Container sizes (standard 20, 40, or 45)."
    )
    customs_seal_numbers: str | None = Field(
        default=None,
        description="Customs seal numbers."
    )

    @field_validator("*", mode="before")
    @classmethod
    def clean_placeholders(cls, v):
        if v is None:
            return None
        if isinstance(v, (list, tuple, set, dict)) and not v:
            return None
        if isinstance(v, (list, tuple, set)):
            non_empty = [
                x for x in v
                if x is not None and str(x).strip().upper() not in (
                    "", "-", "--", "NA", "N/A", "N.A.", "NONE", "NULL", "NIL", "NOT APPLICABLE", "NAN", "."
                )
            ]
            if not non_empty:
                return None
        if isinstance(v, str) and v.strip().upper() in (
            "", "-", "--", "NA", "N/A", "N.A.", "NONE", "NULL", "NIL", "NOT APPLICABLE", "NAN", "."
        ):
            return None
        return v

    @field_validator("shipper_name", mode="before")
    @classmethod
    def clean_shipper(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if str(x).strip())
        s = str(v).strip()
        for _ in range(3):
            s = re.sub(r"^[\s,–—\-]+|[\s,–—\-]+$", "", s)  # noqa: RUF001
            s = s.strip("\"'")
        s = re.sub(r"\s+", " ", s).strip()
        return s or None

    @field_validator("invoice_numbers", mode="before")
    @classmethod
    def clean_invoices(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            val = items[0] if items else ""
        else:
            s = str(v).strip()
            tokens = [t.strip() for t in re.split(r"[\n,]+", s) if t.strip()]
            val = tokens[0] if tokens else ""
        if not val:
            return None
        cleaned = re.split(r"\s+(?:DT\.?|DATED|DATE|AMT\.?|AMOUNT|INR|USD)\b", val, flags=re.IGNORECASE)[0].strip().strip("\"'")
        cleaned = re.sub(r"^[\s,–—\-]+|[\s,–—\-]+$", "", cleaned).strip()  # noqa: RUF001
        return cleaned or None

    @field_validator("leo_date", mode="before")
    @classmethod
    def clean_leo_date(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            val = items[0] if items else ""
        else:
            val = str(v).strip()
        if val.upper() in ("", "-", "--", "NA", "N/A", "N.A.", "NONE", "NULL", "NIL", "NOT APPLICABLE", "NAN", "."):
            return None
        return val or None

    @field_validator("customs_seal_numbers", mode="before")
    @classmethod
    def clean_seals(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            items = [str(x).strip() for x in v if x]
        else:
            items = [str(x).strip() for x in re.split(r"[\n,]+", str(v)) if str(x).strip()]
        valid_items = []
        for item in items:
            cleaned = re.sub(r"^(?:SEAL\s*NO\.?|CUSTOMS\s*SEAL(?:\s*NO\.?)?)\s*[:#-]\s*", "", item, flags=re.IGNORECASE).strip()
            if cleaned.upper() in ("", "-", "--", "PKG", "PACKAGE", "PACKAGES", "SEAL", "SEALS", "NA", "N/A", "N.A.", "NONE", "NULL", "NIL", "NAN", "."):
                continue
            valid_items.append(cleaned)
        if valid_items:
            return "\n".join(valid_items)
        return None

    @field_validator("container_sizes", mode="before")
    @classmethod
    def clean_container_sizes(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            v = "\n".join(str(x) for x in v)
        # Extract only standard sizes 20, 40, 45 (allowing FT, feet, quote annotations)
        tokens = re.findall(r"\b(20|40|45)(?:\s*(?:FT|FEET|'|\"))?(?!\d)", str(v).upper())
        if tokens:
            return "\n".join(tokens)
        return None

    @field_validator("container_numbers", mode="before")
    @classmethod
    def clean_containers(cls, v):
        if not v:
            return None
        if isinstance(v, list):
            v = "\n".join(str(x) for x in v)
        matches = re.findall(r"\b([A-Z]{3}[UJZ])[\s-]?(\d{7})\b", str(v).upper())
        if matches:
            tokens = [f"{prefix}{num}" for prefix, num in matches]
            return "\n".join(dict.fromkeys(tokens))
        return str(v).strip() or None


class ExcelRow(BaseModel):
    """Represents a Shipping Bill row in the Excel Daily Working Report."""
    row_idx: int
    sb_no: str
    sb_date: date | None = None
    inv_no: str | None = None
    leo_date: str | None = None
    shipper_name: str | None = None
    container_no: str | None = None
    container_size: str | None = None
    customs_seal: str | None = None
    missing_fields: list[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields) == 0
