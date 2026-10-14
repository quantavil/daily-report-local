"""Coordinate typed document results; PDF and workbook mechanics stay in adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from daily_report.documents import extract_layout, parse_document, render_markdown
from daily_report.models import BillIdentity, BillResult, FieldResult, RowUpdate, WorkbookRow
from daily_report.rules import (
    BUSINESS_TIMEZONE,
    CONTAINER_ATTRS,
    CONTAINER_FIELDS,
    DATA_FIELDS,
    FIELD_DOCUMENTS,
    LEO_FIELDS,
    discovery_codes,
    document_codes,
    gmail_checks_needed,
    gmail_window_open,
    matches_identity,
)
from daily_report.storage import BillStorage

if TYPE_CHECKING:
    from daily_report.gmail import GmailClient
    from daily_report.workbook import WorkbookStore


def _missing(fields: set[str], reason: str, state: str = 'missing') -> dict[str, FieldResult]:
    return {name: FieldResult(state=state, reason=reason) for name in fields}


def _parse_source(store, raw, metadata, digest, *, refresh):
    layout = None if refresh else store.get_layout(metadata, digest)
    if layout is None or layout.document_code not in (None, metadata.code):
        layout = extract_layout(raw, metadata)
        store.put_layout(metadata, digest, layout)
    parsed = parse_document(layout, metadata, digest)
    if parsed.metadata != metadata or parsed.content_hash != digest:
        raise ValueError('Parser returned mismatching source identity')
    store.put_processed(metadata, digest, parsed, render_markdown(layout, metadata))
    return parsed


def process_bill(
    gmail: GmailClient,
    data_root: Path,
    identity: BillIdentity,
    fields: set[str] | None = None,
    *,
    refresh: bool = False,
    today: date | None = None,
    row_successful: bool = False,
) -> BillResult:
    requested = set(DATA_FIELDS) if fields is None else set(fields)
    if not requested <= set(DATA_FIELDS):
        raise ValueError('Unknown fields: ' + ', '.join(sorted(requested - set(DATA_FIELDS))))
    run_date = today or datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()
    previous = None
    parsed_sources = {}
    if row_successful and not gmail_window_open(identity, run_date):
        try:
            local = BillStorage.discover_local(data_root, identity, set(FIELD_DOCUMENTS.values()))
            identities = {(item.metadata.identity.sb_date, item.metadata.identity.port_code) for item in local}
            if len(identities) == 1:
                previous = BillResult(identity=identity, fields=_missing(set(DATA_FIELDS), 'Stored source missing'))
                latest = {}
                for item in local:
                    if item.metadata.code not in latest or item.metadata.document_at > latest[item.metadata.code].metadata.document_at:
                        latest[item.metadata.code] = item
                for code, item in latest.items():
                    store = BillStorage(data_root, item.metadata.identity)
                    raw = store.get_pdf(item)
                    if raw is None:
                        raise ValueError('Stored source unavailable')
                    digest = sha256(raw).hexdigest()
                    parsed = _parse_source(store, raw, item.metadata, digest, refresh=refresh)
                    parsed_sources[(item.metadata.filename, digest)] = parsed
                    previous.selected_sources.append(item.metadata)
                    previous.selected_hashes[code] = digest
                    previous.fields.update({name: parsed.fields.get(name, FieldResult(state='rejected', reason='Parser omitted owned field'))
                                            for name in DATA_FIELDS if FIELD_DOCUMENTS[name] == code})
                    if code == FIELD_DOCUMENTS['container_no']:
                        previous.containers = parsed.containers
        except Exception:
            # Incomplete or freshly rejected local evidence must reopen Gmail.
            previous = None
    check_gmail = gmail_checks_needed(identity, run_date, previous, row_successful=row_successful)
    missing_reason = 'Matching document has not arrived' if check_gmail else 'Gmail update window closed; no matching stored document'
    result = BillResult(identity=identity, fields=_missing(requested, missing_reason))
    # Full sync checks all document types; LEO-only touches SB22 only.
    codes = discovery_codes(requested)
    if check_gmail:
        discovered = gmail.discover(identity, codes)
    else:
        discovered = []
        for code in sorted(codes):
            try:
                discovered.extend(BillStorage.discover_local(data_root, identity, {code}))
            except Exception as exc:
                reason = f'{code}: {exc}'
                owned = {name for name in requested if FIELD_DOCUMENTS[name] == code}
                result.fields.update(_missing(owned, reason, 'error'))
                result.errors.append(reason)
    attachments = [item for item in discovered
                   if item.metadata.code in codes and matches_identity(identity, item.metadata.identity)]
    # Ignore superseded Gmail attachments before downloads. Equal newest timestamps
    # remain candidates so conflicting content is still reported explicitly.
    newest_times = {}
    for item in attachments:
        key = (item.metadata.identity.sb_date, item.metadata.identity.port_code, item.metadata.code)
        newest_times[key] = max(newest_times.get(key, item.metadata.document_at), item.metadata.document_at)
    attachments = [item for item in attachments if item.metadata.document_at ==
                   newest_times[(item.metadata.identity.sb_date, item.metadata.identity.port_code, item.metadata.code)]]
    if not attachments:
        BillStorage(data_root, identity).put_result(result)
        return result

    retained = []
    stores = {}
    failures: dict[str, str] = {}
    for item in attachments:
        try:
            key = (item.metadata.identity.sb_date, item.metadata.identity.port_code)
            if key not in stores:
                stores[key] = BillStorage(data_root, item.metadata.identity)
            store = stores[key]
            raw = store.get_pdf(item)
            if raw is None:
                if not check_gmail:
                    raise ValueError('Gmail update window closed; stored PDF is missing or corrupt')
                raw = gmail.download(item)
                digest = store.put_pdf(item, raw)
            else:
                digest = sha256(raw).hexdigest()
            store.prune_revisions(item.metadata, digest)
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
        selected = min((item for item in code_metadata if item.metadata.document_at == newest_at),
                       key=lambda item: (item.metadata.filename, item.key))
        result.selected_sources.append(selected.metadata)
        newest_failures = [failures[item.key] for item in code_metadata
                           if item.metadata.document_at == newest_at and item.key in failures]
        if newest_failures:
            result.fields.update(_missing(owned, '; '.join(newest_failures), 'error'))
            continue
        candidates = [entry for entry in retained if entry[0].metadata.code == code]
        if not candidates:
            continue
        newest = [entry for entry in candidates if entry[0].metadata.document_at == newest_at]
        item, store, raw, digest = min(newest, key=lambda entry: (entry[0].metadata.filename, entry[0].key))
        result.selected_hashes[code] = digest
        try:
            parsed = parsed_sources.get((item.metadata.filename, digest))
            if parsed is None:
                parsed = _parse_source(store, raw, item.metadata, digest, refresh=refresh)
            result.fields.update({name: parsed.fields.get(name, FieldResult(state='rejected', reason='Parser omitted owned field'))
                                  for name in owned})
            if set(CONTAINER_FIELDS) & owned:
                result.containers = parsed.containers
        except Exception as exc:
            reason = f'{item.metadata.filename}: {exc}'
            result.fields.update(_missing(owned, reason, 'error'))
            result.errors.append(reason)

    # A scoped result describes this run, never fabricates unprocessed fields.
    # Store observations in their resolved filename batch, retaining workbook identity
    # separately in the result when the row did not supply every component.
    if stores:
        next(iter(stores.values())).put_result(result)
    else:
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
    accepted = {name for name, field in result.fields.items() if name in writable and field.state == 'accepted'}
    values = {name: result.fields[name].value for name in accepted if name not in CONTAINER_FIELDS}
    container_fields = accepted & set(CONTAINER_FIELDS)
    alignment_problem = None
    if container_fields:
        # Compare retained columns with the same ordered records before filling a member.
        for name in CONTAINER_FIELDS:
            existing = row.values.get(name)
            if not existing or (name in container_fields and not str(existing).startswith('=')):
                continue
            observation = result.fields.get(name)
            source = [getattr(record, CONTAINER_ATTRS[name]) or '' for record in result.containers]
            if observation is None or observation.state != 'accepted' or str(existing).split('\n') != source:
                alignment_problem = f'Container records unchanged: retained {name} does not match selected source'
                container_fields = set()
                break
    failures = [f'{name}: {field.reason or field.state}' for name, field in result.fields.items()
                if name in selected and field.state != 'accepted']
    for name in selected - writable:
        field = result.fields.get(name)
        if field is None or field.state != 'accepted':
            continue
        source = '\n'.join(getattr(record, CONTAINER_ATTRS[name]) or '' for record in result.containers) if name in CONTAINER_FIELDS else field.value
        if row.values.get(name) != source:
            failures.append(f'{name}: existing Excel value differs from validated source; use --force to repair')
    if alignment_problem:
        failures.append(alignment_problem)
    states = {field.state for name, field in result.fields.items() if name in selected}
    complete = all(row.values.get(name) or name in values or name in container_fields for name in DATA_FIELDS)
    partial_records = any(
        not getattr(record, CONTAINER_ATTRS[name])
        for name in CONTAINER_FIELDS
        if name in result.fields and result.fields[name].state == 'accepted'
        for record in result.containers
    )
    if 'error' in states or result.errors:
        status = 'Error'
    elif not result.selected_sources and states <= {'missing'}:
        status = 'Not found'
    elif failures or not complete or partial_records:
        status = 'Partial'
    else:
        status = 'Success'
    detail = '; '.join(failures + result.issues + result.errors)
    if not detail:
        detail = f'{len(values) + len(container_fields)} validated field(s) selected'
    return RowUpdate(row_idx=row.row_idx, values=values, containers=result.containers or None,
                     container_fields=container_fields, status=status, detail=detail)


def execute(
    workbook: WorkbookStore,
    sheet: str,
    gmail: GmailClient,
    data_root: Path,
    *,
    selected: list[WorkbookRow],
    force: bool = False,
    leo_only: bool = False,
    workers: int = 3,
    refresh: bool = False,
    verify: bool = False,
) -> list[tuple[WorkbookRow, BillResult | None, RowUpdate]]:
    if not 1 <= workers <= 8:
        raise ValueError('Workers must be between 1 and 8')
    fields = set(LEO_FIELDS) if leo_only else set(DATA_FIELDS)
    records = []
    pending = []
    for row in selected:
        if row.error:
            records.append((row, None, RowUpdate(row_idx=row.row_idx, status='Error', detail=row.error)))
        elif row.cancelled:
            records.append((row, None, RowUpdate(row_idx=row.row_idx, status='Cancelled', detail='Cancelled bill skipped')))
        else:
            pending.append(row)
    # Work once per full identity; repeat workbook rows share source observations.
    groups: dict[tuple, list[WorkbookRow]] = {}
    for row in pending:
        key = (row.identity.sb_no, row.identity.sb_date, row.identity.port_code, row.identity.document_date)
        groups.setdefault(key, []).append(row)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_bill, gmail, data_root, group[0].identity, fields,
                               refresh=refresh, row_successful=all(row.status == 'Success' for row in group)):
                   group for group in groups.values()}
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
