"""Coordinate typed document results; PDF and workbook mechanics stay in adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from daily_report.documents import extract_layout, parse_document, render_markdown, validate_parsed
from daily_report.models import BillIdentity, BillResult, FieldResult, RowUpdate, WorkbookRow
from daily_report.rules import (
    CONTAINER_FIELDS,
    DATA_FIELDS,
    FIELD_DOCUMENTS,
    LEO_FIELDS,
    discovery_codes,
    document_codes,
    matches_identity,
)
from daily_report.storage import BillStorage

if TYPE_CHECKING:
    from daily_report.gmail import GmailClient
    from daily_report.workbook import WorkbookStore


def _missing(fields: set[str], reason: str, state: str = 'missing') -> dict[str, FieldResult]:
    return {name: FieldResult(state=state, reason=reason) for name in fields}


def process_bill(
    gmail: GmailClient,
    data_root: Path,
    identity: BillIdentity,
    fields: set[str] | None = None,
    *,
    refresh: bool = False,
) -> BillResult:
    requested = set(DATA_FIELDS) if fields is None else set(fields)
    if not requested <= set(DATA_FIELDS):
        raise ValueError('Unknown fields: ' + ', '.join(sorted(requested - set(DATA_FIELDS))))
    result = BillResult(identity=identity, fields=_missing(requested, 'Matching document has not arrived'))
    # Full sync retains every matching document type; LEO-only touches SB22 only.
    codes = discovery_codes(requested)
    attachments = [item for item in gmail.discover(identity, codes)
                   if item.metadata.code in codes and matches_identity(identity, item.metadata.identity)]
    if not attachments:
        BillStorage(data_root, identity).put_result(result)
        return result

    retained = []
    failures: dict[str, str] = {}
    for item in attachments:
        try:
            store = BillStorage(data_root, item.metadata.identity)
            raw = store.get_pdf(item)
            if raw is None:
                raw = gmail.download(item)
                digest = store.put_pdf(item, raw)
            else:
                digest = sha256(raw).hexdigest()
            retained.append((item, store, raw, digest))
        except Exception as exc:
            failures[item.key] = f'{item.metadata.filename}: {exc}'
            result.errors.append(failures[item.key])

    identities = {(item.metadata.identity.sb_date, item.metadata.identity.port_code) for item in attachments}
    if len(identities) != 1:
        result.fields = _missing(requested, 'Multiple bill dates/ports match incomplete workbook identity', 'rejected')
        result.issues.append('Conflicting bill identities; fill the SB date and port in Excel')
        BillStorage(data_root, identity).put_result(result)
        return result

    needed_codes = document_codes(requested)
    for code in sorted(needed_codes):
        owned = {name for name in requested if FIELD_DOCUMENTS[name] == code}
        code_metadata = [item for item in attachments if item.metadata.code == code]
        if not code_metadata:
            continue
        newest_at = max(item.metadata.document_at for item in code_metadata)
        newest_failures = [failures[item.key] for item in code_metadata
                           if item.metadata.document_at == newest_at and item.key in failures]
        if newest_failures:
            result.fields.update(_missing(owned, '; '.join(newest_failures), 'error'))
            continue
        candidates = [entry for entry in retained if entry[0].metadata.code == code]
        if not candidates:
            continue
        newest = [entry for entry in candidates if entry[0].metadata.document_at == newest_at]
        if len({entry[3] for entry in newest}) != 1:
            result.fields.update(_missing(owned, f'{code}: same document timestamp has different PDF content', 'rejected'))
            result.issues.append(f'{code}: ambiguous revision')
            continue
        item, store, raw, digest = min(newest, key=lambda entry: (entry[0].metadata.filename, entry[0].key))
        result.selected_sources.append(item.metadata)
        result.selected_hashes[code] = digest
        try:
            layout = store.get_layout(digest)
            if layout is None or layout.document_code not in (None, code):
                layout = extract_layout(raw, item.metadata)
                store.put_layout(digest, layout)
            parsed = None if refresh else store.get_parsed(digest)
            if parsed is None or parsed.metadata != item.metadata or parsed.content_hash != digest or not validate_parsed(parsed, layout):
                parsed = parse_document(layout, item.metadata, digest)
                markdown = render_markdown(raw, item.metadata)
                store.put_processed(digest, parsed, markdown)
            if parsed.metadata != item.metadata or parsed.content_hash != digest:
                raise ValueError('Parser returned mismatching source identity')
            result.fields.update({name: parsed.fields.get(name, FieldResult(state='rejected', reason='Parser omitted owned field'))
                                  for name in owned})
            if set(CONTAINER_FIELDS) & owned:
                result.containers = parsed.containers
        except Exception as exc:
            reason = f'{item.metadata.filename}: {exc}'
            result.fields.update(_missing(owned, reason, 'error'))
            result.errors.append(reason)

    # A scoped result describes this run, never fabricates unprocessed fields.
    BillStorage(data_root, identity).put_result(result)
    return result


def make_update(
    row: WorkbookRow,
    result: BillResult,
    fields: set[str] | None = None,
    *,
    force: bool = False,
) -> RowUpdate:
    selected = set(DATA_FIELDS) if fields is None else set(fields)
    writable = selected if force else {name for name in selected if not row.values.get(name)}
    values = {name: field.value for name, field in result.fields.items()
              if name in writable and field.state == 'accepted'}
    alignment_problem = None
    container_fields = set(CONTAINER_FIELDS)
    if container_fields & values.keys():
        # Retained cells are part of the same record: don't attach a new seal/size
        # to an unrelated existing container, including a rejected force target.
        for name in container_fields:
            existing = row.values.get(name)
            if not existing or (name in values and not str(existing).startswith('=')):
                continue
            observation = result.fields.get(name)
            source = observation.value if observation is not None and observation.state == 'accepted' else None
            source_text = '\n'.join(item or '' for item in source) if isinstance(source, list) else source
            if existing != source_text:
                alignment_problem = f'Container records unchanged: retained {name} does not match selected source'
                values = {key: value for key, value in values.items() if key not in container_fields}
                break
    merged = row.values | values
    failures = [f'{name}: {field.reason or field.state}' for name, field in result.fields.items()
                if name in writable and field.state != 'accepted']
    if alignment_problem:
        failures.append(alignment_problem)
    states = {field.state for name, field in result.fields.items() if name in writable}
    if 'error' in states or result.errors:
        status = 'Error'
    elif not result.selected_sources and states <= {'missing'}:
        status = 'Not found'
    elif failures or any(not merged.get(name) for name in DATA_FIELDS) or any(
        isinstance(field.value, list) and any(not item for item in field.value)
        for field in result.fields.values() if field.state == 'accepted'
    ):
        status = 'Partial'
    else:
        status = 'Success'
    detail = '; '.join(failures + result.issues + result.errors)
    if not detail:
        detail = f'{len(values)} validated field(s) selected'
    return RowUpdate(row_idx=row.row_idx, values=values, containers=result.containers or None,
                     status=status, detail=detail)


def execute(
    workbook: WorkbookStore,
    sheet: str,
    gmail: GmailClient,
    data_root: Path,
    *,
    row_numbers: set[int] | None = None,
    sb_no: str | None = None,
    force: bool = False,
    leo_only: bool = False,
    workers: int = 3,
    refresh: bool = False,
    verify: bool = False,
) -> list[tuple[WorkbookRow, BillResult | None, RowUpdate]]:
    if not 1 <= workers <= 8:
        raise ValueError('Workers must be between 1 and 8')
    fields = set(LEO_FIELDS) if leo_only else set(DATA_FIELDS)
    selected = workbook.rows(sheet, row_numbers=row_numbers, sb_no=sb_no)
    records = []
    pending = []
    for row in selected:
        if row.error:
            records.append((row, None, RowUpdate(row_idx=row.row_idx, status='Error', detail=row.error)))
        elif row.cancelled:
            records.append((row, None, RowUpdate(row_idx=row.row_idx, status='Cancelled', detail='Cancelled bill skipped')))
        elif verify or force or any(not row.values.get(name) for name in fields):
            pending.append(row)
    # Work once per full identity; repeat workbook rows share source observations.
    groups: dict[tuple, list[WorkbookRow]] = {}
    for row in pending:
        key = (row.identity.sb_no, row.identity.sb_date, row.identity.port_code, row.identity.document_date)
        groups.setdefault(key, []).append(row)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_bill, gmail, data_root, group[0].identity, fields,
                               refresh=refresh or verify): group for group in groups.values()}
        for future in as_completed(futures):
            group = futures[future]
            try:
                result = future.result()
                records.extend((row, result, make_update(row, result, fields, force=force or verify)) for row in group)
            except Exception as exc:
                records.extend((row, None, RowUpdate(row_idx=row.row_idx, status='Error', detail=str(exc))) for row in group)
    records.sort(key=lambda entry: entry[0].row_idx)
    if not verify and records:
        workbook.apply(sheet, [record[2] for record in records], force=force, fields=fields)
        workbook.save()
    return records
