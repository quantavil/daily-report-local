"""Validates AI-selected LEO dates against their cited source Markdown."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from excel_manager import coerce_excel_date
from models import LeoDateEvidence

_LABEL_PATTERNS = {
    "EGATEPASS": {
        "1ORDERNODATE": re.compile(r"(?<![\w])1\s*\.\s*Order\s+No\s*/\s*date", re.IGNORECASE),
    },
    "FINAL_LEO": {
        "6LEODATE": re.compile(r"(?<![\w])6\s*\.\s*LEO\s*Date\s*\.?", re.IGNORECASE),
        "9LEO": re.compile(r"(?<![\w])9\s*\.\s*LEO\b", re.IGNORECASE),
    },
}
_NEXT_FIELD_LABEL_RE = re.compile(
    r"(?<![\w])(?:[A-Z]\s+)?\d{1,2}\s*\.\s*[A-Za-z][A-Za-z0-9 /.-]{1,50}",
    re.IGNORECASE,
)
_MERGED_ORDER_OFFICER_HEADER_RE = re.compile(
    r"1\s*\.\s*order\s+no\s*/\s*date\s+2\s*\.\s*name\s+of\s+officer",
    re.IGNORECASE,
)
_ORDER_ID_RE = re.compile(r"\d{1,4}\s*/\s*\d{1,4}")
_OTHER_DATE_LABEL_RE = re.compile(
    r"(?<![a-z0-9])(?:\d{1,2}\s*\.\s*)?(?:SB|CIN|Submission|BRC|Assessment)"
    r"(?:\s+(?:No|Date|DT))?(?![a-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LeoDateValidation:
    leo_date: str | None
    warning: str | None = None
    accepted_evidence: list[LeoDateEvidence] = field(default_factory=list)


def _markdown_normalize(value: str) -> str:
    """Removes basic Markdown table/emphasis syntax and collapses whitespace."""
    plain = value.replace("|", " ").replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", plain).strip().casefold()


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _markdown_normalize(value)).upper()


def _label_key(doc_type: str, label: str) -> str | None:
    compact = _compact(label)
    for key in _LABEL_PATTERNS.get(doc_type, {}):
        if compact == key:
            return key
    return None


def _document_anchors(slot: str, page_text: str) -> set[tuple[str, str]]:
    doc_type = {"eGatepass": "EGATEPASS", "finalLeo": "FINAL_LEO"}.get(slot)
    if not doc_type:
        return set()
    source = _markdown_normalize(page_text)
    return {(doc_type, key) for key, pattern in _LABEL_PATTERNS[doc_type].items() if pattern.search(source)}


def _date_from_token(token: str) -> date | None:
    parsed = coerce_excel_date(token)
    return parsed.date() if isinstance(parsed, datetime) else parsed if isinstance(parsed, date) else None


def _table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_markdown_normalize(cell) for cell in line.split("|")]


def _merged_gatepass_order_id(page_text: str, raw_date: str | None) -> str | None:
    """Validates the one observed AnyDoc merged order/officer table row."""
    rows = []
    for line in page_text.splitlines():
        if "|" not in line:
            continue
        cells = _table_cells(line)
        header_columns = [
            index for index, cell in enumerate(cells) if _MERGED_ORDER_OFFICER_HEADER_RE.fullmatch(cell)
        ]
        if header_columns:
            rows.append((cells, header_columns))
    if len(rows) != 1:
        return None

    cells, header_columns = rows[0]
    if len(cells) < 7 or header_columns != [2] or cells[0] != "a leo dtls":
        return None
    allowed_columns = {0, 2, 3, 4}
    if raw_date is not None:
        allowed_columns.add(6)
    if any(cell for index, cell in enumerate(cells) if index not in allowed_columns):
        return None

    if not _ORDER_ID_RE.fullmatch(cells[3]):
        return None

    officer = cells[4]
    if not officer or _date_from_token(officer) is not None or _NEXT_FIELD_LABEL_RE.match(officer):
        return None
    if _OTHER_DATE_LABEL_RE.search(officer):
        return None

    if raw_date is None:
        if cells[6]:
            return None
    else:
        date_cell = cells[6]
        if date_cell != _markdown_normalize(raw_date):
            return None
        if _date_from_token(date_cell) is None:
            return None

    return cells[3]


def _has_merged_gatepass_header(page_text: str) -> bool:
    return any(
        _MERGED_ORDER_OFFICER_HEADER_RE.fullmatch(cell)
        for line in page_text.splitlines()
        if "|" in line
        for cell in _table_cells(line)
    )


def _value_segments(page_text: str, doc_type: str, label_key: str) -> list[str]:
    """Preserve cell/field boundaries when checking an AI-selected value."""
    pattern = _LABEL_PATTERNS[doc_type][label_key]
    raw_lines = [line for line in page_text.splitlines() if line.strip()]
    lines = [_markdown_normalize(line) for line in raw_lines]
    segments = []
    for index, line in enumerate(lines):
        # A table header maps to the SAME column on the next data row.
        # Flattening the row here would borrow the first column's date.
        if "|" in raw_lines[index]:
            cells = _table_cells(raw_lines[index])
            populated = [cell for cell in cells if cell]
            next_cells = []
            for next_raw in raw_lines[index + 1:]:
                if "|" not in next_raw:
                    break
                candidate = _table_cells(next_raw)
                if candidate and all(re.fullmatch(r":?-{3,}:?", cell) for cell in candidate):
                    continue
                next_cells = candidate
                break
            header = (
                len(populated) > 1
                and all(_NEXT_FIELD_LABEL_RE.match(cell) for cell in populated)
                and any(next_cells)
                and not any(_NEXT_FIELD_LABEL_RE.search(cell) for cell in next_cells)
            )
            if header:
                for column, cell in enumerate(cells):
                    if pattern.search(cell):
                        value = next_cells[column] if len(next_cells) == len(cells) else "<ambiguous table>"
                        segments.append(value)
                continue

        for match in pattern.finditer(line):
            tail = line[match.end():]
            # AnyDoc can merge the event row's non-date SEZ label into the
            # event cell. Only this observed row-label fold is supported.
            if doc_type == "FINAL_LEO" and label_key == "9LEO" and "|" in raw_lines[index]:
                tail = re.sub(r"^\s*10\.\s*SEZ\s+UNIT\s+Details\b", "", tail, flags=re.IGNORECASE)
            following = _NEXT_FIELD_LABEL_RE.search(tail)
            segment = tail[:following.start() if following else len(tail)].strip(" :.")
            if not segment and following is None and index + 1 < len(lines):
                # Only an unambiguous single-field text line can borrow its
                # direct next value line. Multi-cell rows require cell mapping.
                if "|" in raw_lines[index]:
                    cells = _table_cells(raw_lines[index])
                    column = next((i for i, cell in enumerate(cells) if pattern.search(cell)), -1)
                    segments.append("" if column >= 0 and column + 1 < len(cells) else "<ambiguous table>")
                    continue
                next_line = lines[index + 1]
                next_field = _NEXT_FIELD_LABEL_RE.search(next_line)
                segment = next_line[:next_field.start() if next_field else len(next_line)].strip(" :.")
            segments.append(segment)
    return segments


def _associated_value(page_text: str, doc_type: str, label_key: str, raw_date: str | None) -> bool:
    segments = _value_segments(page_text, doc_type, label_key)
    if doc_type == "EGATEPASS":
        segments = [re.sub(r"^\d+/\d+(?:\s+|$)", "", value).strip() for value in segments]
    if raw_date is None:
        return len(segments) == 1 and not segments[0]
    token = _markdown_normalize(raw_date)
    if not token:
        return False
    return any(re.match(rf"{re.escape(token)}(?![a-z0-9])", value) for value in segments)


def _validate_observation(
    evidence: LeoDateEvidence,
    contexts: dict[str, dict[int, str]],
) -> tuple[str, date | None] | None:
    slot_by_type = {"EGATEPASS": "eGatepass", "FINAL_LEO": "finalLeo"}
    slot = slot_by_type.get(evidence.doc_type)
    if slot is None or evidence.page_number != 1:
        return None

    page_text = contexts.get(slot, {}).get(evidence.page_number)
    if not page_text:
        return None

    label_key = _label_key(evidence.doc_type, evidence.label)
    if label_key is None or (evidence.doc_type, label_key) not in _document_anchors(slot, page_text):
        return None

    quote = _markdown_normalize(evidence.quote)
    source = _markdown_normalize(page_text)
    if not quote or quote not in source:
        return None
    if evidence.doc_type == "EGATEPASS" and _has_merged_gatepass_header(page_text):
        source_order_id = _merged_gatepass_order_id(page_text, evidence.raw_date)
        quote_order_id = _merged_gatepass_order_id(evidence.quote, evidence.raw_date)
        if source_order_id is None or quote_order_id != source_order_id:
            return None
    elif not _associated_value(page_text, evidence.doc_type, label_key, evidence.raw_date):
        return None
    elif not _associated_value(evidence.quote, evidence.doc_type, label_key, evidence.raw_date):
        return None
    if evidence.raw_date is None:
        return label_key, None
    parsed_date = _date_from_token(evidence.raw_date)
    return (label_key, parsed_date) if parsed_date is not None else None


def resolve_leo_date(
    evidence: list[LeoDateEvidence],
    contexts: dict[str, dict[int, str]],
) -> LeoDateValidation:
    """Returns a date only when every eligible source observation is cited and agrees."""
    eligible_contexts = {
        slot: {page: value for page, value in pages.items() if page == 1 and value.strip()}
        for slot, pages in contexts.items()
        if slot in ("eGatepass", "finalLeo")
    }
    eligible_contexts = {slot: pages for slot, pages in eligible_contexts.items() if pages}
    if not eligible_contexts:
        return LeoDateValidation(None)

    required_anchors = {
        anchor
        for slot, pages in eligible_contexts.items()
        for anchor in _document_anchors(slot, pages[1])
    }
    if not required_anchors:
        return LeoDateValidation(None, "eligible documents did not contain a supported LEO/order label")
    if not evidence:
        return LeoDateValidation(None, "AI returned no LEO evidence for eligible source labels")

    for slot, pages in eligible_contexts.items():
        doc_type = {"eGatepass": "EGATEPASS", "finalLeo": "FINAL_LEO"}[slot]
        source = _markdown_normalize(pages[1])
        if any(len(list(pattern.finditer(source))) > 1 for pattern in _LABEL_PATTERNS[doc_type].values()):
            return LeoDateValidation(None, "ambiguous repeated LEO/order labels on source page")

    validated: list[tuple[LeoDateEvidence, tuple[str, str], date | None]] = []
    for item in evidence:
        observation = _validate_observation(item, eligible_contexts)
        if observation is None:
            return LeoDateValidation(None, f"unverified LEO evidence for {item.doc_type} page {item.page_number}")
        label_key, parsed_date = observation
        validated.append((item, (item.doc_type, label_key), parsed_date))

    observed_anchors = {anchor for _item, anchor, _parsed in validated}
    if observed_anchors != required_anchors:
        missing = required_anchors - observed_anchors
        return LeoDateValidation(None, f"AI evidence omitted eligible LEO observation(s): {sorted(missing)}")

    distinct_dates = {parsed for _item, _anchor, parsed in validated if parsed is not None}
    if not distinct_dates:
        return LeoDateValidation(None, "eligible LEO/order fields are blank")
    if len(distinct_dates) != 1:
        return LeoDateValidation(None, "conflicting LEO dates across eligible source observations")

    validated.sort(
        key=lambda entry: (
            entry[2] is None,
            0 if entry[0].doc_type == "EGATEPASS" else 1,
            {"6LEODATE": 0, "9LEO": 1, "1ORDERNODATE": 0}.get(entry[1][1], 9),
        )
    )
    chosen_date = distinct_dates.pop()
    return LeoDateValidation(
        chosen_date.strftime("%d-%b-%Y"),
        accepted_evidence=[item for item, _anchor, _parsed in validated],
    )
