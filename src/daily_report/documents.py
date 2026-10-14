"""Native PDF geometry and rule-driven source association."""

from __future__ import annotations

import pymupdf

from daily_report import rules
from daily_report.models import (
    ContainerRecord,
    DocumentLayout,
    DocumentMetadata,
    Evidence,
    FieldResult,
    LayoutPage,
    LayoutSpan,
    ParsedDocument,
)


def _open(pdf):
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:
        raise RuntimeError("Unreadable PDF") from exc
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise RuntimeError("Encrypted PDF")
    return doc


def _eligible(number, code):
    pages = rules.PAGES[code]
    return pages is None or number in pages


def extract_layout(pdf: bytes, metadata: DocumentMetadata) -> DocumentLayout:
    doc = _open(pdf)
    try:
        pages = []
        for i, page in enumerate(doc):
            spans = [
                LayoutSpan(text=s["text"], bbox=s["bbox"])
                for block in page.get_text("dict", sort=False)["blocks"]
                for line in block.get("lines", [])
                for s in line["spans"]
                if s["text"].strip()
            ]
            if not spans and not _eligible(i + 1, metadata.code):
                continue
            if not spans:
                raise RuntimeError(f"Page {i + 1} has no native text; OCR is disabled")
            pages.append(LayoutPage(number=i + 1, width=page.rect.width, height=page.rect.height, spans=spans))
        if not pages:
            raise RuntimeError("PDF contains no eligible pages")
        return DocumentLayout(document_code=metadata.code, pages=pages)
    finally:
        doc.close()


def render_markdown(layout: DocumentLayout, metadata: DocumentMetadata) -> str:
    """Render inspection text from native cells, retaining original page numbers."""
    chunks = []
    for page in layout.pages:
        if _eligible(page.number, metadata.code):
            text = "\n\n".join(cell.text for cell in _lines(page))
            chunks.append(f"--- SOURCE PAGE {page.number} ---\n{text}")
    return "\n\n".join(chunks)


def _box(spans):
    return (
        min(s.bbox[0] for s in spans),
        min(s.bbox[1] for s in spans),
        max(s.bbox[2] for s in spans),
        max(s.bbox[3] for s in spans),
    )


def _lines(page):
    """Join adjacent fragments only; unrelated columns retain separate cells."""
    rows = []
    for span in sorted(page.spans, key=lambda s: (s.bbox[1], s.bbox[0])):
        matching = next((r for r in rows if abs(r[0].bbox[1] - span.bbox[1]) <= rules.ROW_TOLERANCE), None)
        if matching is None:
            rows.append([span])
        else:
            matching.append(span)
    cells = []
    for row in rows:
        current = []
        for span in sorted(row, key=lambda s: s.bbox[0]):
            if current and span.bbox[0] - current[-1].bbox[2] > rules.MAX_INLINE_GAP:
                cells.append(LayoutSpan(text=" ".join(s.text for s in current), bbox=_box(current)))
                current = []
            current.append(span)
        if current:
            cells.append(LayoutSpan(text=" ".join(s.text for s in current), bbox=_box(current)))
    return cells


def _labels(page, field):
    # Original complete spans prevent a close adjacent value from swallowing its label.
    candidates = [s for s in page.spans if rules.label_matches(field, s.text)]
    for cell in _lines(page):
        if rules.label_matches(field, cell.text) and not any(s.bbox == cell.bbox for s in candidates):
            candidates.append(cell)
    return candidates


def _boundaries(page):
    labels = [
        s
        for s in page.spans
        if rules.NUMBERED_LABEL.match(s.text) or any(rules.label_matches(f, s.text) for f in rules.LABELS)
    ]
    for field in rules.LABELS:
        for source in _labels(page, field):
            if source not in labels:
                labels.append(source)
    for prefix in page.spans:
        if not rules.NUMBERED_PREFIX.fullmatch(prefix.text.strip()):
            continue
        neighbors = sorted(
            [
                s
                for s in page.spans
                if s.bbox[0] >= prefix.bbox[2] and abs(s.bbox[1] - prefix.bbox[1]) <= rules.ROW_TOLERANCE
            ],
            key=lambda s: s.bbox[0],
        )
        if neighbors and neighbors[0].bbox[0] - prefix.bbox[2] <= rules.MAX_INLINE_GAP:
            merged = LayoutSpan(text=prefix.text + " " + neighbors[0].text, bbox=_box([prefix, neighbors[0]]))
            if rules.NUMBERED_LABEL.match(merged.text):
                labels.append(merged)
    return labels


def _cell(page, label, field):
    labels = _boundaries(page)

    def is_label(span):
        return any(
            source.bbox[0] <= span.bbox[0]
            and source.bbox[2] >= span.bbox[2]
            and abs(source.bbox[1] - span.bbox[1]) <= rules.ROW_TOLERANCE
            for source in labels
        )

    right = min(
        [
            s.bbox[0]
            - max((rules.CELL_LEFT_MARGIN.get(f, 0) for f in rules.LABELS if rules.label_matches(f, s.text)), default=0)
            for s in labels
            if abs(s.bbox[1] - label.bbox[1]) <= rules.ROW_TOLERANCE and s.bbox[0] >= label.bbox[2]
        ]
        or [page.width]
    )
    inline = [
        s
        for s in page.spans
        if abs(s.bbox[1] - label.bbox[1]) <= rules.ROW_TOLERANCE
        and label.bbox[2] <= s.bbox[0] < right
        and not is_label(s)
    ]
    if inline and field in rules.INLINE_VALUE_FIELDS:
        return sorted(inline, key=lambda s: s.bbox[0])
    left = label.bbox[0] - rules.CELL_LEFT_MARGIN.get(field, rules.COLUMN_TOLERANCE)
    bottom = min(
        [
            s.bbox[1]
            for s in labels
            if s.bbox[1] > label.bbox[1] + rules.ROW_TOLERANCE and s.bbox[0] < right and s.bbox[2] > left
        ]
        or [label.bbox[3] + rules.MAX_CELL_DEPTH]
    )
    candidates = [
        s
        for s in page.spans
        if s.bbox[1] > label.bbox[1] + rules.ROW_TOLERANCE
        and s.bbox[1] < min(bottom, label.bbox[3] + rules.MAX_CELL_DEPTH)
        and left <= s.bbox[0] < right
        and not is_label(s)
    ]
    if not candidates:
        return []
    top = min(s.bbox[1] for s in candidates)
    return sorted([s for s in candidates if abs(s.bbox[1] - top) <= rules.ROW_TOLERANCE], key=lambda s: s.bbox[0])


def _evidence(page, label, values, metadata, content_hash):
    return Evidence(
        filename=metadata.filename,
        content_hash=content_hash,
        page_number=page.number,
        label=label.text,
        quote=" ".join(s.text for s in values),
        bbox=_box([label, *values]),
    )


def _field(pages, field, metadata, content_hash):
    hits = [(page, label) for page in pages for label in _labels(page, field)]
    if field in rules.SOURCE_PAGE_PRIORITY and hits:
        priority = rules.SOURCE_PAGE_PRIORITY[field]
        selected_page = min((p.number for p, _ in hits), key=priority.index)
        hits = [(p, label) for p, label in hits if p.number == selected_page]
    if len(hits) != 1:
        return FieldResult(state="rejected", reason="Eligible source label missing or ambiguous")
    page, label = hits[0]
    values = _cell(page, label, field)
    if not values:
        return FieldResult(state="blank", reason="Eligible source cell is empty")
    try:
        value = rules.value_token(field, " ".join(s.text for s in values))
    except ValueError as exc:
        return FieldResult(state="rejected", reason=str(exc))
    evidence = [_evidence(page, label, values, metadata, content_hash)]
    return (
        FieldResult(state="accepted", value=value, evidence=evidence)
        if value is not None
        else FieldResult(state="blank", evidence=evidence)
    )


def _identity(pages, metadata, content_hash):
    # Port/date labels in cargo/item tables are not bill identity. Associate them
    # with a supported bill header band or the numbered secondary header profile.
    primary_count = 0
    for page in pages:
        for anchor in _labels(page, "sb_no"):
            sources = {}
            for field in rules.IDENTITY_FIELDS:
                labels = [
                    source
                    for source in _labels(page, field)
                    if abs(source.bbox[1] - anchor.bbox[1]) <= rules.ROW_TOLERANCE
                ]
                if len(labels) == 1:
                    sources[field] = labels[0]
            if all(field in sources for field in rules.IDENTITY_FIELDS):
                primary_count += 1
            elif rules.secondary_identity_label(anchor.text, "anchor"):
                profile = rules.SECONDARY_IDENTITY
                companions = [
                    source
                    for source in _labels(page, profile["field"])
                    if rules.secondary_identity_label(source.text, "companion")
                    and abs(source.bbox[0] - anchor.bbox[0]) <= rules.COLUMN_TOLERANCE
                    and abs(source.bbox[1] - anchor.bbox[1] - profile["row_offset"]) <= rules.ROW_TOLERANCE
                ]
                if len(companions) != 1:
                    return "PDF secondary identity header missing or ambiguous"
                sources = {"sb_no": anchor, profile["field"]: companions[0]}
            else:
                return "PDF identity header missing or ambiguous"
            for field, source in sources.items():
                expected = getattr(metadata.identity, field)
                if expected is None:
                    continue
                values = _cell(page, source, field)
                try:
                    actual = rules.value_token(field, " ".join(s.text for s in values))
                except ValueError:
                    return "PDF identity source is invalid"
                if actual != expected:
                    return "PDF identity conflicts with filename"
    return None if primary_count else "PDF primary identity header missing"


def _stacked_records(page, headers, metadata, content_hash, rejected):
    records = []
    starts = sorted(headers["container_no"], key=lambda s: s.bbox[1])
    for i, label in enumerate(starts):
        bottom = starts[i + 1].bbox[1] if i + 1 < len(starts) else label.bbox[1] + rules.MAX_CELL_DEPTH
        values = {}
        evidence = []
        for field in rules.CONTAINER_FIELDS:
            eligible = [s for s in headers[field] if label.bbox[1] <= s.bbox[1] < bottom]
            if len(eligible) != 1:
                raise ValueError("Stacked movement record has missing or ambiguous source header")
            source = eligible[0]
            cell = _cell(page, source, field)
            try:
                value = rules.value_token(field, " ".join(s.text for s in cell)) if cell else None
            except ValueError as exc:
                rejected[field] = str(exc)
                value = None
            values[rules.CONTAINER_ATTRS[field]] = value
            if cell:
                evidence.append(_evidence(page, source, cell, metadata, content_hash))
        records.append(ContainerRecord(**values, evidence=evidence))
    return records


def _movement(pages, metadata, content_hash):
    records = []
    rejected = {}
    columns = None
    stacked_seen = False
    for page in pages:
        headers = {f: _labels(page, f) for f in rules.CONTAINER_FIELDS}
        if headers["container_no"] and headers["customs_seal"] and headers["container_size"]:
            ys = [s.bbox[1] for values in headers.values() for s in values]
            if max(ys) - min(ys) > rules.ROW_TOLERANCE:
                records.extend(_stacked_records(page, headers, metadata, content_hash, rejected))
                stacked_seen = True
                continue
        if any(headers.values()):
            if not all(len(v) == 1 for v in headers.values()):
                raise ValueError("Incomplete or ambiguous movement headers")
            columns = {f: v[0] for f, v in headers.items()}
            if (
                max(s.bbox[1] for s in columns.values()) - min(s.bbox[1] for s in columns.values())
                > rules.ROW_TOLERANCE
            ):
                raise ValueError("Movement headers are not aligned")
            top = max(s.bbox[3] for s in columns.values())
        elif columns:
            identity_headers = [label for f in rules.IDENTITY_FIELDS for label in _labels(page, f)]
            top = max(
                (
                    s.bbox[3]
                    for label in identity_headers
                    for s in [
                        label,
                        *_cell(
                            page, label, next(f for f in rules.IDENTITY_FIELDS if rules.label_matches(f, label.text))
                        ),
                    ]
                ),
                default=0,
            )
            # Inherited columns are valid only for rows with an aligned container cell.
            anchors = [
                s
                for s in page.spans
                if s.bbox[1] >= top and abs(s.bbox[0] - columns["container_no"].bbox[0]) <= rules.COLUMN_TOLERANCE
            ]
            valid = []
            for s in anchors:
                try:
                    if rules.value_token("container_no", s.text) is not None:
                        valid.append(s)
                except ValueError:
                    pass
            if not valid:
                # A text-bearing page after a movement table is unresolved unless it
                # is an explicit terminal page; never silently discard table data.
                if any(s.bbox[1] >= top and not rules.MOVEMENT_STOP.match(s.text) for s in page.spans):
                    raise ValueError("Unsupported movement continuation geometry")
                continue
            if any(
                rules.MOVEMENT_STOP.match(s.text) and s.bbox[1] >= top and s.bbox[1] < max(a.bbox[1] for a in valid)
                for s in page.spans
            ):
                raise ValueError("Movement continuation has intervening unsupported section")
        else:
            if stacked_seen:
                for source in page.spans:
                    try:
                        candidate = rules.value_token("container_no", source.text)
                    except ValueError:
                        continue
                    if candidate is not None:
                        raise ValueError("Headerless stacked movement continuation requires supported source geometry")
            continue
        order = sorted(columns, key=lambda f: columns[f].bbox[0])
        rows = []
        for s in sorted(page.spans, key=lambda s: (s.bbox[1], s.bbox[0])):
            if s.bbox[1] < top:
                continue
            if rules.MOVEMENT_STOP.match(s.text):
                for later in page.spans:
                    if later.bbox[1] <= s.bbox[1]:
                        continue
                    try:
                        candidate = rules.value_token("container_no", later.text)
                    except ValueError:
                        continue
                    if candidate is not None:
                        raise ValueError("Movement records occur after unsupported section boundary")
                break
            matching = next((r for r in rows if abs(r[0].bbox[1] - s.bbox[1]) <= rules.ROW_TOLERANCE), None)
            if matching is None:
                rows.append([s])
            else:
                matching.append(s)
        for row in rows:
            values = {}
            evidence = []
            for i, f in enumerate(order):
                left = columns[f].bbox[0] - rules.COLUMN_TOLERANCE
                right = columns[order[i + 1]].bbox[0] - rules.COLUMN_TOLERANCE if i + 1 < len(order) else page.width
                cell = [s for s in row if left <= s.bbox[0] < right]
                if cell:
                    try:
                        values[rules.CONTAINER_ATTRS[f]] = rules.value_token(f, " ".join(s.text for s in cell))
                    except ValueError as exc:
                        rejected[f] = str(exc)
                        values[rules.CONTAINER_ATTRS[f]] = None
                    evidence.append(_evidence(page, columns[f], cell, metadata, content_hash))
            if values:
                records.append(ContainerRecord(**values, evidence=evidence))
    if columns is None and not records:
        raise ValueError("Movement table headers missing")
    return records, rejected


def parse_document(layout: DocumentLayout, metadata: DocumentMetadata, content_hash: str) -> ParsedDocument:
    pages = [p for p in layout.pages if _eligible(p.number, metadata.code)]
    fields = {
        f: FieldResult(state="rejected", reason="No eligible native layout")
        for f, c in rules.FIELD_DOCUMENTS.items()
        if c == metadata.code
    }
    result = ParsedDocument(metadata=metadata, content_hash=content_hash, fields=fields)
    reason = _identity(layout.pages, metadata, content_hash)
    if reason:
        result.fields = {f: FieldResult(state="rejected", reason=reason) for f in fields}
        return result
    if any(f in rules.CONTAINER_FIELDS for f in fields):
        try:
            result.containers, rejected = _movement(pages, metadata, content_hash)
        except ValueError as exc:
            result.fields = {f: FieldResult(state="rejected", reason=str(exc)) for f in fields}
            return result
        for f in fields:
            if f in rejected:
                result.fields[f] = FieldResult(state="rejected", reason=rejected[f])
                continue
            attr = rules.CONTAINER_ATTRS[f]
            evidence = [e for r in result.containers for e in r.evidence if rules.label_matches(f, e.label)]
            values = [getattr(r, attr) for r in result.containers]
            result.fields[f] = (
                FieldResult(
                    state="accepted",
                    value_type="records",
                    evidence=evidence,
                    reason="Movement records contain verified empty positions" if not all(values) else None,
                )
                if values and any(values)
                else FieldResult(state="blank", evidence=evidence, reason="Movement records contain empty positions")
            )
    else:
        result.fields = {f: _field(pages, f, metadata, content_hash) for f in fields}
    return result
