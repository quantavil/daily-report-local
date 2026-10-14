from datetime import date

import pytest
from pydantic import ValidationError

from daily_report.models import BillIdentity, ContainerRecord, FieldResult


def test_bill_identity_rejects_path_components():
    with pytest.raises(ValidationError):
        BillIdentity(sb_no='../bad')


def test_identity_normalizes_port():
    assert BillIdentity(sb_no='7546404', port_code='inmbd6').port_code == 'INMBD6'


def test_accepted_value_requires_evidence():
    with pytest.raises(ValidationError):
        FieldResult(state='accepted', value=date(2026, 10, 8))


def test_accepted_container_outcome_references_records_without_copying_values():
    from daily_report.models import Evidence
    evidence = Evidence(filename='source.pdf', content_hash='hash', page_number=1,
                        label='11.Container', quote='ABCU1234567', bbox=(0, 0, 10, 10))
    result = FieldResult(state='accepted', value_type='records', evidence=[evidence])
    assert result.value is None
    assert FieldResult.model_validate_json(result.model_dump_json()).value_type == 'records'
    with pytest.raises(ValidationError):
        FieldResult(state='accepted', value_type='records', value='ABCU1234567', evidence=[evidence])
    with pytest.raises(ValidationError):
        FieldResult(state='accepted', value_type='records')


def test_container_missing_positions_are_preserved():
    records = [ContainerRecord(number='ABCU1234567', size='20'), ContainerRecord(number='DEFU7654321', seal='X')]
    assert [r.size for r in records] == ['20', None]
    assert [r.seal for r in records] == [None, 'X']


def test_json_preserves_date_and_date_shaped_invoice_types():
    from daily_report.models import Evidence
    evidence = Evidence(filename='source.pdf', content_hash='hash', page_number=1,
                        label='label', quote='quote', bbox=(0, 0, 10, 10))
    dated = FieldResult(state='accepted', value=date(2026, 10, 8), evidence=[evidence])
    invoice = FieldResult(state='accepted', value='2026-10-08', evidence=[evidence])
    assert FieldResult.model_validate_json(dated.model_dump_json()).value == date(2026, 10, 8)
    assert FieldResult.model_validate_json(invoice.model_dump_json()).value == '2026-10-08'


def test_attachment_identity_uses_stable_mime_part_not_download_token():
    from datetime import datetime

    from daily_report.models import Attachment, DocumentMetadata
    metadata = DocumentMetadata(identity=BillIdentity(sb_no='7546404'), code='SB22',
                                document_at=datetime(2026, 10, 8), filename='source.pdf')
    first = Attachment(metadata=metadata, message_id='message', part_id='1', attachment_id='old-token')
    second = first.model_copy(update={'attachment_id': 'new-token'})
    assert first.key == second.key
    assert first.key != first.model_copy(update={'part_id': '2'}).key
