import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from config import EXCEL_COLUMNS, REQUIRED_DATA_FIELDS
from models import CustomsExtractedFields, ExcelRow


def is_empty_cell(val: Any) -> bool:
    """Checks if a cell value is empty, blank, or placeholder (case-insensitive)."""
    if val is None:
        return True
    s = str(val).strip().upper()
    return s in ("", "-", "--", "NA", "N/A", "N.A.", "NONE", "NULL", "NIL", "NOT APPLICABLE", "NAN", ".")


def build_merged_cell_map(ws: Worksheet) -> dict[tuple[int, int], tuple[int, int]]:
    """Builds a fast (row, col) -> (top_row, top_col) mapping for all merged child cells."""
    m = {}
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    m[(r, c)] = (rng.min_row, rng.min_col)
    return m


def get_effective_cell_value(
    ws: Worksheet,
    row: int,
    col: int,
    merged_map: dict[tuple[int, int], tuple[int, int]] | None = None,
) -> Any:
    """Returns the effective value of a cell, resolving merged cells from the top-left cell."""
    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        if merged_map and (row, col) in merged_map:
            top_r, top_c = merged_map[(row, col)]
            return ws.cell(row=top_r, column=top_c).value
        for rng in ws.merged_cells.ranges:
            if row in range(rng.min_row, rng.max_row + 1) and col in range(rng.min_col, rng.max_col + 1):
                return ws.cell(row=rng.min_row, column=rng.min_col).value
    return cell.value


def coerce_excel_date(val: Any) -> Any:
    """
    Converts 'DD-Mon-YYYY' / 'DD/MM/YYYY' / 'YYYY-MM-DD' strings to datetime.date
    so the LEO column keeps real Excel dates (matching existing rows). Passes
    through dates/datetimes, returns anything else unchanged.
    """
    if val is None or isinstance(val, (date, datetime)):
        return val
    s = str(val).strip()
    m = re.match(r"^(\d{1,2})[-/\.]([A-Za-z]{3})[-/\.](\d{2,4})$", s)
    if m:
        dd, mon, yyyy = m.groups()
        months = {mth.lower(): i + 1 for i, mth in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
        mi = months.get(mon.lower())
        if mi:
            try:
                y = int(yyyy)
                if y < 100:
                    y += 2000 if y <= 49 else 1900
                return date(y, mi, int(dd))
            except ValueError:
                return val
    m = re.match(r"^(\d{1,2})[-/\.](\d{1,2})[-/\.](\d{2,4})$", s)
    if m:
        dd, mm, yyyy = m.groups()
        try:
            y = int(yyyy)
            if y < 100:
                y += 2000 if y <= 49 else 1900
            return date(y, int(mm), int(dd))
        except ValueError:
            return val
    m = re.match(r"^(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})$", s)
    if m:
        yyyy, mm, dd = m.groups()
        try:
            return date(int(yyyy), int(mm), int(dd))
        except ValueError:
            return val
    return val


def excel_cell_to_str(v: Any) -> str:
    """Canonical string form of an Excel cell value (stolen from gstr-wala).

    Integer-valued floats lose the trailing '.0' (1278254.0 -> "1278254"),
    dates/datetimes become DD-Mon-YYYY strings, None becomes "". This kills
    the ".0"-poisoned SB-number bug: str(1278254.0) is "1278254.0", which
    digit-stripping turns into 8-digit "12782540" and the row gets skipped.
    """
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%b-%Y")
    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return ""
        return str(int(v)) if v.is_integer() else str(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        return s[:-2]
    return s


def is_cancelled_value(v: Any) -> bool:
    """Checks if a billing status or remarks value indicates a cancelled shipping bill."""
    if v is None:
        return False
    return "CANCEL" in str(v).strip().upper()


class ExcelManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.wb: Workbook | None = None

    def load(self, read_only: bool = False):
        # NOTE: never use read_only=True for data ops — ReadOnlyWorksheet
        # has no random cell access (each ws.cell(row=N) re-streams from
        # the top, turning a 1483-row scan into a multi-minute hang).
        # Normal load is ~1.3s for this 630KB workbook, so always use it.
        # The read_only flag is kept for API compat but ignored.
        self.wb = openpyxl.load_workbook(self.file_path, data_only=False, read_only=False)

    def save(self, target_path: Path | None = None):
        out_path = target_path or self.file_path
        if not self.wb:
            return
        if Path(out_path) == Path(self.file_path):
            self._backup_in_place()
        self.wb.save(out_path)

    def _backup_in_place(self, keep: int = 5):
        """Timestamped backup before overwriting the only copy of the workbook.

        A crash mid-save would otherwise corrupt the workbook. Keeps the
        newest `keep` backups next to the original:
        `<stem>.bak-<YYYYMMDD-HHMMSS>.xlsx`.
        """
        import shutil
        src = Path(self.file_path)
        if not src.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = src.with_name(f"{src.stem}.bak-{stamp}{src.suffix}")
        shutil.copy2(src, backup)
        older = sorted(
            src.parent.glob(f"{src.stem}.bak-*{src.suffix}"),
            key=lambda p: p.name,
        )
        for stale in older[:-keep] if len(older) > keep else []:
            try:
                stale.unlink()
            except OSError:
                pass

    def ensure_status_column(self, sheet_name: str) -> int:
        """Replaces the 'Additional Remarks' column with a 'Status' column.

        Returns the 1-based Status column index. An existing 'Status' column
        is reused; otherwise 'Additional Remarks' (if any) is removed and
        'Status' takes its place; else 'Status' is appended at the end.
        """
        if not self.wb:
            self.load()
        ws = self.wb[sheet_name]
        headers = [
            re.sub(r"\s+", " ", str(ws.cell(row=1, column=c).value or "")).strip().lower().rstrip(".:")
            for c in range(1, ws.max_column + 1)
        ]
        if "status" in headers:
            idx = headers.index("status") + 1
            self.style_status_header(ws, idx)
            return idx
        if "additional remarks" in headers:
            idx = headers.index("additional remarks") + 1
            ws.delete_cols(idx)
            ws.insert_cols(idx)
            ws.cell(row=1, column=idx).value = "Status"
            self.style_status_header(ws, idx)
            return idx
        ws.cell(row=1, column=ws.max_column + 1).value = "Status"
        self.style_status_header(ws, ws.max_column)
        return ws.max_column

    @staticmethod
    def style_status_header(ws: Worksheet, idx: int):
        """Styles the Status header like its neighbours, but a distinct color.

        Copies font/alignment/border from the header on the left so it blends
        in, with a dark-green fill so the system column stands out slightly.
        """
        from copy import copy

        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        cell = ws.cell(row=1, column=idx)
        left = ws.cell(row=1, column=idx - 1) if idx > 1 else None
        name = left.font.name if left and left.font.name else "Calibri"
        cell.font = Font(bold=True, color="FFFFFFFF", size=11, name=name)
        cell.alignment = copy(left.alignment) if left else Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = copy(left.border) if left else Border(
            left=Side(style="thin"), right=Side(style="thin"),
            top=Side(style="thin"), bottom=Side(style="thin"),
        )
        cell.fill = PatternFill(patternType="solid", fgColor="FF276738")

    def create_template_sheet(self, name: str = "Template", source: str = "FY 2026-27") -> bool:
        """Creates a header-only template sheet as the first tab.

        Copies header values, styles, column widths, row height, freeze panes
        and the header filter from `source`. Returns False if it exists.
        Duplicate it (right-click tab) to start a new FY sheet.
        """
        from copy import copy

        from openpyxl.utils import get_column_letter

        if not self.wb:
            self.load()
        if name in self.wb.sheetnames:
            return False
        src = self.wb[source] if source in self.wb.sheetnames else self.wb.active
        ws = self.wb.create_sheet(name, 0)
        for c in range(1, src.max_column + 1):
            dst = ws.cell(row=1, column=c)
            dst.value = src.cell(row=1, column=c).value
            dst.font = copy(src.cell(row=1, column=c).font)
            dst.fill = copy(src.cell(row=1, column=c).fill)
            dst.alignment = copy(src.cell(row=1, column=c).alignment)
            dst.border = copy(src.cell(row=1, column=c).border)
            letter = get_column_letter(c)
            ws.column_dimensions[letter].width = src.column_dimensions[letter].width
        ws.row_dimensions[1].height = src.row_dimensions[1].height
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(src.max_column)}1"
        return True

    def write_status(self, sheet_name: str, row_idx: int, col_idx: int, text: str) -> bool:
        """Writes the sync status for a row (always overwrites, never a formula)."""
        if not self.wb:
            self.load()
        ws = self.wb[sheet_name]
        cell = ws.cell(row=row_idx, column=col_idx)
        if isinstance(cell, MergedCell):
            merged_map = build_merged_cell_map(ws)
            if (row_idx, col_idx) in merged_map:
                top_r, top_c = merged_map[(row_idx, col_idx)]
                cell = ws.cell(row=top_r, column=top_c)
            else:
                return False
        if str(cell.value or "").startswith("="):
            return False
        cell.value = text
        return True

    def get_sheet_names(self) -> list[str]:
        if not self.wb:
            self.load()
        return self.wb.sheetnames

    def resolve_columns(self, ws: Worksheet) -> dict[str, int]:
        """Maps logical field names to 1-based column indices based on header row."""
        header_row = ws[1]
        headers = [
            re.sub(r"\s+", " ", str(c.value or "")).strip().lower().rstrip(".:")
            for c in header_row
        ]

        mapping: dict[str, int] = {}
        for key, aliases in EXCEL_COLUMNS.items():
            for alias in aliases:
                norm_alias = re.sub(r"\s+", " ", alias).strip().lower().rstrip(".:")
                for idx, h in enumerate(headers):
                    if norm_alias == h:
                        mapping[key] = idx + 1
                        break
                if key in mapping:
                    break
        return mapping

    def find_missing_rows(self, sheet_name: str) -> tuple[list[ExcelRow], dict[str, int]]:
        """
        Scans all data rows in sheet_name and returns rows that have an SB number
        but are missing one or more required fields.
        """
        if not self.wb:
            self.load()

        ws = self.wb[sheet_name]
        col_map = self.resolve_columns(ws)
        merged_map = build_merged_cell_map(ws)

        sb_col = col_map.get("sb_no")
        sb_date_col = col_map.get("sb_date")
        if not sb_col:
            raise ValueError(f"Could not find 'Shipping Bill No' column in sheet '{sheet_name}'")

        missing_rows: list[ExcelRow] = []

        for row_idx in range(2, ws.max_row + 1):
            sb_cell_val = get_effective_cell_value(ws, row_idx, sb_col, merged_map)
            if not sb_cell_val:
                continue

            sb_no = excel_cell_to_str(sb_cell_val)
            if not sb_no:
                continue
            # Ensure it's a 7-digit shipping bill number
            sb_clean = re.sub(r"[^0-9]", "", sb_no)
            if len(sb_clean) != 7:
                continue

            raw_sb_date = get_effective_cell_value(ws, row_idx, sb_date_col, merged_map) if sb_date_col else None
            coerced_date = coerce_excel_date(raw_sb_date)
            parsed_sb_date = coerced_date.date() if isinstance(coerced_date, datetime) else (coerced_date if isinstance(coerced_date, date) else None)

            # Skip cancelled shipping bills (e.g. Billing Status == CANCELLED)
            billing_col = col_map.get("billing_status")
            if billing_col:
                b_val = get_effective_cell_value(ws, row_idx, billing_col, merged_map)
                if is_cancelled_value(b_val):
                    continue

            missing_fields = []
            for f in REQUIRED_DATA_FIELDS:
                col = col_map.get(f)
                if not col:
                    continue
                val = get_effective_cell_value(ws, row_idx, col, merged_map)
                if is_empty_cell(val):
                    missing_fields.append(f)

            if missing_fields:
                inv_no = str(get_effective_cell_value(ws, row_idx, col_map.get("inv_no", 0), merged_map) or "") if col_map.get("inv_no") else None
                leo_date = str(get_effective_cell_value(ws, row_idx, col_map.get("leo_date", 0), merged_map) or "") if col_map.get("leo_date") else None
                shipper_name = str(get_effective_cell_value(ws, row_idx, col_map.get("shipper_name", 0), merged_map) or "") if col_map.get("shipper_name") else None
                container_no = str(get_effective_cell_value(ws, row_idx, col_map.get("container_no", 0), merged_map) or "") if col_map.get("container_no") else None
                container_size = str(get_effective_cell_value(ws, row_idx, col_map.get("container_size", 0), merged_map) or "") if col_map.get("container_size") else None
                customs_seal = str(get_effective_cell_value(ws, row_idx, col_map.get("customs_seal", 0), merged_map) or "") if col_map.get("customs_seal") else None

                missing_rows.append(ExcelRow(
                    row_idx=row_idx,
                    sb_no=sb_clean,
                    sb_date=parsed_sb_date,
                    inv_no=inv_no,
                    leo_date=leo_date,
                    shipper_name=shipper_name,
                    container_no=container_no,
                    container_size=container_size,
                    customs_seal=customs_seal,
                    missing_fields=missing_fields
                ))

        return missing_rows, col_map

    def apply_updates(
        self,
        sheet_name: str,
        row_idx: int,
        updates: CustomsExtractedFields,
        col_map: dict[str, int],
        force_overwrite: bool = False,
        fields: set[str] | None = None,
    ) -> list[str]:
        """
        Safely writes extracted values into cells for row_idx.
        Preserves existing values unless empty or force_overwrite is True.
        Handles openpyxl MergedCell instances safely without crashing.
        """
        ws = self.wb[sheet_name]
        merged_map = build_merged_cell_map(ws)
        applied: list[str] = []

        coerced_leo = coerce_excel_date(updates.leo_date)
        leo_date_val = coerced_leo if isinstance(coerced_leo, (date, datetime)) else None

        field_mapping = {
            "inv_no": updates.invoice_numbers,
            "leo_date": leo_date_val,
            "shipper_name": updates.shipper_name,
            "container_no": updates.container_numbers,
            "container_size": updates.container_sizes,
            "customs_seal": updates.customs_seal_numbers,
        }

        if fields is not None and not fields <= field_mapping.keys():
            raise ValueError("Unknown update fields: " + ", ".join(sorted(fields - field_mapping.keys())))

        for field_name, new_val in field_mapping.items():
            if fields is not None and field_name not in fields:
                continue
            if not new_val:
                continue
            col = col_map.get(field_name)
            if not col:
                continue

            cell = ws.cell(row=row_idx, column=col)
            if isinstance(cell, MergedCell):
                effective_val = get_effective_cell_value(ws, row_idx, col, merged_map)
                if not force_overwrite and not is_empty_cell(effective_val):
                    continue
                if (row_idx, col) in merged_map:
                    top_r, top_c = merged_map[(row_idx, col)]
                    cell = ws.cell(row=top_r, column=top_c)
                else:
                    continue

            # Never overwrite formulas
            if str(cell.value or "").startswith("="):
                continue

            if force_overwrite or is_empty_cell(cell.value):
                cell.value = new_val
                if isinstance(new_val, (date, datetime)) and field_name == "leo_date":
                    cell.number_format = "DD-MMM-YYYY"
                if "\n" in str(new_val):
                    from openpyxl.styles import Alignment
                    current_align = cell.alignment
                    if current_align:
                        cell.alignment = Alignment(
                            horizontal=current_align.horizontal,
                            vertical=current_align.vertical,
                            wrap_text=True,
                        )
                    else:
                        cell.alignment = Alignment(wrap_text=True)
                applied.append(f"{field_name}={new_val}")

        return applied
