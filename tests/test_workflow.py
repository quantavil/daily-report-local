from datetime import date, datetime

import pytest

from daily_report.models import (
    Attachment,
    BillIdentity,
    ContainerRecord,
    DocumentLayout,
    DocumentMetadata,
    Evidence,
    FieldResult,
    LayoutPage,
    LayoutSpan,
    ParsedDocument,
    WorkbookRow,
)
from daily_report.workflow import make_update, process_bill

IDENTITY = BillIdentity(sb_no='7546404', sb_date=date(2026, 10, 7), port_code='INMBD6')


def attachment(code='SB22', stamp='081020261717', port='INMBD6', message='m'):
    filename = f'754640407102026{port}{code}{stamp}.pdf'
    meta = DocumentMetadata(identity=IDENTITY.model_copy(update={'port_code': port}), code=code,
                            filename=filename, document_at=datetime.strptime(stamp, '%d%m%Y%H%M'))
    return Attachment(metadata=meta, message_id=message, attachment_id='a')


class Mail:
    def __init__(self, attachments):
        self.attachments = attachments
        self.downloads = 0
        self.discoveries = 0

    def discover(self, identity, codes):
        self.discoveries += 1
        return [a for a in self.attachments if a.metadata.code in codes]

    def download(self, item):
        self.downloads += 1
        return b'%PDF-' + item.metadata.filename.encode() + item.message_id.encode()


@pytest.fixture
def parser(monkeypatch):
    from daily_report import workflow
    calls = {'layout': 0, 'parse': 0}

    def layout(raw, meta):
        calls['layout'] += 1
        return DocumentLayout(pages=[LayoutPage(number=1, width=600, height=800,
             spans=[LayoutSpan(text='Evidence', bbox=(10, 10, 100, 20))])])

    def parse(layout, meta, digest):
        calls['parse'] += 1
        evidence = Evidence(filename=meta.filename, content_hash=digest, page_number=1,
                            label='Source', quote='Evidence', bbox=(10, 10, 100, 20))
        values = {'SB21': {'shipper_name': 'Exporter', 'inv_no': 'INV/1'},
                  'SB22': {'leo_date': date(2026, 10, 8)},
                  'SB35': {'container_no': 'ABCU1234567', 'container_size': '20', 'customs_seal': 'SEAL1'}}
        fields = {name: FieldResult(state='accepted', value=value, evidence=[evidence])
                  for name, value in values[meta.code].items()}
        containers = [ContainerRecord(number='ABCU1234567', size='20', seal='SEAL1', evidence=[evidence])] if meta.code == 'SB35' else []
        return ParsedDocument(metadata=meta, content_hash=digest, fields=fields, containers=containers)

    monkeypatch.setattr(workflow, 'extract_layout', layout)
    monkeypatch.setattr(workflow, 'parse_document', parse)
    monkeypatch.setattr(workflow, 'validate_parsed', lambda parsed, layout: True)
    monkeypatch.setattr(workflow, 'render_markdown', lambda raw, meta: 'Evidence')
    return calls


def test_first_copy_fills_only_owned_fields(tmp_path, parser):
    result = process_bill(Mail([attachment('SB21')]), tmp_path, IDENTITY)
    assert result.fields['inv_no'].value == 'INV/1'
    assert result.fields['leo_date'].state == 'missing'
    row = WorkbookRow(row_idx=2, identity=IDENTITY)
    update = make_update(row, result)
    assert update.status == 'Partial'
    assert set(update.values) == {'inv_no', 'shipper_name'}


def test_incremental_reuses_artifacts_but_discovers_fresh(tmp_path, parser):
    mail = Mail([attachment('SB22')])
    first = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    second = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    assert first == second
    assert mail.discoveries == 2
    assert mail.downloads == 1
    assert parser == {'layout': 1, 'parse': 1}


def test_refresh_reparses_without_redownloading(tmp_path, parser):
    mail = Mail([attachment('SB22')])
    process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    process_bill(mail, tmp_path, IDENTITY, {'leo_date'}, refresh=True)
    assert mail.downloads == 1
    assert parser['parse'] == 2


def test_filename_timestamp_beats_mail_arrival_order(tmp_path, parser):
    older = attachment(stamp='071020261000', message='late-email')
    newer = attachment(stamp='081020261000', message='early-email')
    result = process_bill(Mail([older, newer]), tmp_path, IDENTITY, {'leo_date'})
    assert result.selected_sources[0].filename == newer.metadata.filename
    assert len(list(tmp_path.rglob('*.pdf'))) == 2


def test_equal_timestamps_different_content_reject(tmp_path, parser):
    mail = Mail([attachment(message='m1'), attachment(message='m2')])
    result = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    assert result.fields['leo_date'].state == 'rejected'
    assert parser['parse'] == 0


def test_equal_timestamp_identical_content_is_duplicate(tmp_path, parser):
    mail = Mail([attachment(message='m1'), attachment(message='m2')])
    mail.download = lambda item: b'identical'
    result = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    assert result.fields['leo_date'].state == 'accepted'


def test_wrong_port_never_processed(tmp_path, parser):
    result = process_bill(Mail([attachment(port='INXXX6')]), tmp_path, IDENTITY)
    assert all(field.state == 'missing' for field in result.fields.values())
    assert parser['parse'] == 0


def test_unknown_port_multiple_bill_identities_reject(tmp_path, parser):
    expected = IDENTITY.model_copy(update={'port_code': None})
    result = process_bill(Mail([attachment(), attachment(port='INXXX6')]), tmp_path, expected)
    assert all(field.state == 'rejected' for field in result.fields.values())
    assert parser['parse'] == 0


def test_conversion_failure_preserves_existing_date(tmp_path, parser, monkeypatch):
    from daily_report import workflow
    monkeypatch.setattr(workflow, 'extract_layout', lambda *a: (_ for _ in ()).throw(ValueError('OCR disabled')))
    result = process_bill(Mail([attachment()]), tmp_path, IDENTITY, {'leo_date'})
    update = make_update(WorkbookRow(row_idx=2, identity=IDENTITY, values={'leo_date': date(2026, 10, 1)}),
                         result, {'leo_date'}, force=True)
    assert update.status == 'Error'
    assert not update.values


def test_outside_requested_fields_are_never_written(tmp_path, parser):
    result = process_bill(Mail([attachment('SB21'), attachment('SB22')]), tmp_path, IDENTITY)
    update = make_update(WorkbookRow(row_idx=2, identity=IDENTITY), result, {'leo_date'}, force=True)
    assert set(update.values) == {'leo_date'}


def test_complete_existing_row_still_partial_on_rejected_force(tmp_path, parser):
    result = process_bill(Mail([attachment(message='m1'), attachment(message='m2')]), tmp_path, IDENTITY, {'leo_date'})
    row = WorkbookRow(row_idx=2, identity=IDENTITY, values={
        'inv_no': 'I', 'shipper_name': 'E', 'leo_date': date(2026, 10, 1),
        'container_no': 'C', 'container_size': '20', 'customs_seal': 'S'})
    assert make_update(row, result, {'leo_date'}, force=True).status == 'Partial'


def test_no_documents_not_found(tmp_path, parser):
    result = process_bill(Mail([]), tmp_path, IDENTITY)
    assert make_update(WorkbookRow(row_idx=2, identity=IDENTITY), result).status == 'Not found'


def book(tmp_path, cancelled=False):
    import openpyxl

    from daily_report.workbook import WorkbookStore
    path = tmp_path / 'report.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'FY 2026-27'
    ws.append(['Shipping Bill No', 'Shipping Bill Date', 'Port Code', 'Shipper Name', 'Inv No',
               'LEO Date', 'Container No', 'Container Size', 'Customs Seal', 'Billing Status', 'Status'])
    for _ in range(2):
        ws.append(['7546404', date(2026, 10, 7), 'INMBD6', None, None, None, None, None, None,
                   'CANCELLED' if cancelled else None, None])
    wb.save(path)
    return WorkbookStore(path)


def test_execute_writes_only_selected_rows(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    mail = Mail([attachment('SB21'), attachment('SB22'), attachment('SB35')])
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={3})
    assert [entry[0].row_idx for entry in records] == [3]
    assert records[0][2].status == 'Success'
    rows = wb.rows('FY 2026-27')
    assert rows[0].values['leo_date'] is None
    assert rows[1].values['leo_date'] == date(2026, 10, 8)
    assert wb.wb['FY 2026-27'].cell(2, 11).value is None


def test_execute_verification_reparses_without_workbook_save(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    before = wb.path.read_bytes()
    mail = Mail([attachment('SB22')])
    execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={2}, leo_only=True)
    before = wb.path.read_bytes()
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={2}, leo_only=True, verify=True)
    assert records[0][1].fields['leo_date'].value == date(2026, 10, 8)
    assert wb.path.read_bytes() == before
    assert parser['parse'] == 2
    assert len(list(tmp_path.glob('*.bak-*.xlsx'))) == 1


def test_cancelled_rows_do_not_access_gmail(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path, cancelled=True)
    mail = Mail([])
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={2})
    assert records[0][2].status == 'Cancelled'
    assert mail.discoveries == 0
    assert wb.wb['FY 2026-27'].cell(3, 11).value is None


def test_duplicate_workbook_bill_processed_once(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    mail = Mail([attachment('SB22')])
    execute(wb, 'FY 2026-27', mail, tmp_path / 'data', leo_only=True)
    assert mail.discoveries == 1
    assert parser['parse'] == 1


def test_force_leo_only_preserves_other_fields(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    ws = wb.wb['FY 2026-27']
    ws.cell(2, 4).value = 'Existing exporter'
    ws.cell(2, 6).value = date(2026, 10, 1)
    mail = Mail([attachment('SB21'), attachment('SB22')])
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={2}, leo_only=True, force=True)
    assert ws.cell(2, 4).value == 'Existing exporter'
    assert ws.cell(2, 6).value == date(2026, 10, 8)
    assert [meta.code for meta in records[0][1].selected_sources] == ['SB22']


def test_discovery_failure_is_error_not_not_found(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    mail = Mail([])
    mail.discover = lambda *args: (_ for _ in ()).throw(RuntimeError('Gmail unavailable'))
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', row_numbers={2})
    assert records[0][2].status == 'Error'
    assert wb.wb['FY 2026-27'].cell(2, 6).value is None


def test_old_download_failure_does_not_block_newest_evidence(tmp_path, parser):
    older = attachment(stamp='071020261000', message='old')
    newer = attachment(stamp='081020261000', message='new')
    mail = Mail([older, newer])
    def download(item):
        if item.message_id == 'old':
            raise RuntimeError('Old attachment unavailable')
        return b'newest'
    mail.download = download
    result = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    assert result.fields['leo_date'].state == 'accepted'
    assert result.selected_sources == [newer.metadata]
    update = make_update(WorkbookRow(row_idx=2, identity=IDENTITY), result)
    assert update.values['leo_date'] == date(2026, 10, 8)
    assert update.status == 'Error'  # Failed retention still reported explicitly.


def test_failed_newest_does_not_fall_back_to_older(tmp_path, parser):
    mail = Mail([attachment(stamp='071020261000', message='old'), attachment(message='new')])
    def download(item):
        if item.message_id == 'new':
            raise RuntimeError('Newest unavailable')
        return b'old'
    mail.download = download
    result = process_bill(mail, tmp_path, IDENTITY, {'leo_date'})
    assert result.fields['leo_date'].state == 'error'
    assert parser['parse'] == 0


def test_missing_seal_never_attaches_to_different_existing_container(tmp_path, parser):
    result = process_bill(Mail([attachment('SB35')]), tmp_path, IDENTITY)
    row = WorkbookRow(row_idx=2, identity=IDENTITY,
                      values={'container_no': 'DEFU7654321', 'container_size': '40'})
    update = make_update(row, result)
    assert 'customs_seal' not in update.values
    assert 'Container records unchanged' in update.detail
    assert update.status == 'Partial'


def test_matching_existing_container_allows_its_missing_seal(tmp_path, parser):
    result = process_bill(Mail([attachment('SB35')]), tmp_path, IDENTITY)
    row = WorkbookRow(row_idx=2, identity=IDENTITY,
                      values={'container_no': 'ABCU1234567', 'container_size': '20'})
    update = make_update(row, result)
    assert update.values['customs_seal'] == 'SEAL1'


def test_partial_record_sequence_remains_partial():
    from daily_report.models import BillResult
    ev = Evidence(filename='source.pdf', content_hash='hash', page_number=1,
                  label='12.Seal', quote='X', bbox=(0, 0, 10, 10))
    result = BillResult(identity=IDENTITY, selected_sources=[attachment('SB35').metadata], fields={
        'customs_seal': FieldResult(state='accepted', value=[None, 'X'], evidence=[ev])})
    row = WorkbookRow(row_idx=2, identity=IDENTITY, values={
        'inv_no':'I', 'shipper_name':'E', 'leo_date':date(2026,10,8),
        'container_no':'ABCU1234567\nDEFU7654321', 'container_size':'20\n20'})
    assert make_update(row, result, {'customs_seal'}, force=True).status == 'Partial'


def test_selected_identity_error_does_not_fetch_or_block_other_bill(tmp_path, parser):
    from daily_report.workflow import execute
    wb = book(tmp_path)
    ws = wb.wb['FY 2026-27']
    ws.cell(2, 2).value = 'not a date'
    mail = Mail([attachment('SB22')])
    records = execute(wb, 'FY 2026-27', mail, tmp_path / 'data', leo_only=True)
    assert records[0][2].status == 'Error'
    assert records[1][1].fields['leo_date'].state == 'accepted'
    assert mail.discoveries == 1


def test_force_does_not_pair_new_seal_with_protected_container_formula(tmp_path, parser):
    result = process_bill(Mail([attachment('SB35')]), tmp_path, IDENTITY)
    row = WorkbookRow(row_idx=2, identity=IDENTITY, values={'container_no': '=A2', 'container_size': '20'})
    update = make_update(row, result, force=True)
    assert not {'container_no', 'container_size', 'customs_seal'} & update.values.keys()
    assert update.status == 'Partial'
