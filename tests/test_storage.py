"""Readable per-document artifacts retain validated provenance without a manifest."""
import base64
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from daily_report import rules
from daily_report.models import Attachment, BillIdentity, BillResult, DocumentLayout, FieldResult, ParsedDocument
from daily_report.storage import BillStorage, StorageError

IDENTITY = BillIdentity(sb_no='7546404', sb_date=date(2026, 10, 7), port_code='INMBD6')


def attachment(message='one', part='1'):
    return Attachment(message_id=message, part_id=part, attachment_id='download-v1',
        metadata=rules.parse_filename('754640407102026INMBD6SB22081020260915.pdf'))


def record_path(store):
    return store.path / 'processed' / (attachment().metadata.filename[:-4] + '.json')


def test_flat_batch_and_document_json_initialized_before_parsing(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    digest = store.put_pdf(attachment(), b'first')
    assert store.path == tmp_path / '2026' / '754640407102026INMBD6'
    record = json.loads(record_path(store).read_text())
    assert record['content_hash'] == digest
    assert record['metadata']['filename'] == attachment().metadata.filename
    assert record['sources'] == [{'message_id': 'one', 'part_id': '1'}]
    assert store.get_pdf(attachment()) == b'first'
    assert not list(tmp_path.rglob('manifest.json'))


def test_filename_collision_reports_error_without_overwriting(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(attachment(), b'first')
    with pytest.raises(StorageError, match='different content'):
        store.put_pdf(attachment('other'), b'second')
    assert store.get_pdf(attachment()) == b'first'
    assert store.get_pdf(attachment('other')) is None
    assert len(list(tmp_path.rglob('*.pdf'))) == 1


def test_corrupt_retained_pdf_can_be_restored_from_matching_download(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    digest = store.put_pdf(attachment(), b'original')
    (store.path / 'pdfs' / attachment().metadata.filename).write_bytes(b'corrupt')
    assert store.get_pdf(attachment()) is None
    assert store.put_pdf(attachment(), b'original') == digest
    assert store.get_pdf(attachment()) == b'original'


def test_same_bytes_can_have_multiple_sources_and_rotating_tokens(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: BillStorage(tmp_path, IDENTITY).put_pdf(attachment(str(n)), b'first'), range(16)))
    for n in range(16):
        assert store.get_pdf(attachment(str(n)).model_copy(update={'attachment_id': 'rotated'})) == b'first'
    assert store.get_pdf(attachment('0', 'other-part')) is None
    assert len(json.loads(record_path(store).read_text())['sources']) == 16


def test_identity_folders_include_unknowns_with_year_grouping(tmp_path):
    identities = [IDENTITY, IDENTITY.model_copy(update={'sb_date': date(2026, 10, 9)}),
                  IDENTITY.model_copy(update={'port_code': 'INMBD7'}), BillIdentity(sb_no='7546404')]
    stores = [BillStorage(tmp_path, identity) for identity in identities]
    assert len({store.path for store in stores}) == 4
    assert stores[-1].path.name == '7546404unknownunknown'
    assert all(store.path.parent == tmp_path / '2026' for store in stores[:3])
    assert stores[-1].path.parent == tmp_path / 'unknown'
    with pytest.raises(StorageError, match='identity'):
        stores[1].put_pdf(attachment(), b'first')


def test_corrupt_pdf_json_metadata_and_inline_data_are_cache_misses(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(attachment(), b'first')
    current = attachment().model_copy(update={'inline_data': base64.urlsafe_b64encode(b'changed').decode()})
    assert store.get_pdf(current) is None
    matching = current.model_copy(update={'inline_data': base64.urlsafe_b64encode(b'first').decode()})
    assert store.get_pdf(matching) == b'first'
    record = json.loads(record_path(store).read_text())
    record['metadata']['code'] = 'SB21'
    record_path(store).write_text(json.dumps(record))
    assert store.get_pdf(attachment()) is None
    store.put_pdf(attachment(), b'first')
    (store.path / 'pdfs' / attachment().metadata.filename).write_bytes(b'corrupt')
    assert store.get_pdf(attachment()) is None
    record_path(store).write_text('{bad')
    assert store.get_pdf(attachment()) is None


def test_single_json_preserves_layout_sources_and_processing_output(tmp_path, monkeypatch):
    store = BillStorage(tmp_path, IDENTITY)
    item = attachment()
    digest = store.put_pdf(item, b'first')
    layout = DocumentLayout()
    parsed = ParsedDocument(metadata=item.metadata, content_hash=digest, fields={'leo_date': FieldResult(state='blank')})
    store.put_layout(item.metadata, digest, layout)
    store.put_processed(item.metadata, digest, parsed, '# Inspection')
    store.put_pdf(attachment('new-source'), b'first')
    assert store.get_layout(item.metadata, digest) == layout
    record = json.loads(record_path(store).read_text())
    assert record['fields'] == {'leo_date': parsed.fields['leo_date'].model_dump(mode='json')}
    assert record['containers'] == []
    assert record['layout'] == layout.model_dump(mode='json')
    assert record['parser_version'] == rules.PARSER_VERSION
    assert len(record['sources']) == 2
    assert record_path(store).with_suffix('.md').read_text() == '# Inspection'
    assert len(list((store.path / 'processed').iterdir())) == 2
    monkeypatch.setattr(rules, 'PARSER_VERSION', 'changed')
    assert store.get_layout(item.metadata, digest) == layout
    monkeypatch.setattr(rules, 'LAYOUT_VERSION', 'changed')
    assert store.get_layout(item.metadata, digest) is None


def test_layout_invalidates_on_rules_and_native_converter_version(tmp_path, monkeypatch):
    from daily_report import storage
    store = BillStorage(tmp_path, IDENTITY)
    item = attachment()
    digest = store.put_pdf(item, b'first')
    store.put_layout(item.metadata, digest, DocumentLayout())
    monkeypatch.setattr(rules, 'RULES_VERSION', 'changed')
    assert store.get_layout(item.metadata, digest) is None
    store.put_layout(item.metadata, digest, DocumentLayout())
    monkeypatch.setattr(storage, 'package_version', lambda _: 'changed')
    assert store.get_layout(item.metadata, digest) is None


def test_processing_requires_retained_matching_document(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    digest = 'a' * 64
    with pytest.raises(StorageError, match='retained'):
        store.put_layout(attachment().metadata, digest, DocumentLayout())
    digest = store.put_pdf(attachment(), b'first')
    parsed = ParsedDocument(metadata=attachment().metadata, content_hash='b' * 64)
    with pytest.raises(StorageError, match='hash'):
        store.put_processed(attachment().metadata, digest, parsed, '')


def test_result_only_and_permissions_atomic_failure(tmp_path, monkeypatch):
    store = BillStorage(tmp_path / 'data', IDENTITY)
    digest = store.put_pdf(attachment(), b'first')
    result = BillResult(identity=IDENTITY, selected_sources=[attachment().metadata], selected_hashes={'SB22': digest})
    store.put_result(result)
    assert json.loads((store.path / 'result.json').read_text())['selected_hashes'] == {'SB22': digest}
    for path in (tmp_path / 'data').rglob('*'):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    monkeypatch.setattr(store, '_atomic_write', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(StorageError, match='disk full'):
        store.put_result(result)


def revision(code='SB22', time='0915'):
    return Attachment(message_id=f'{code}-{time}', part_id='1',
        metadata=rules.parse_filename(f'754640407102026INMBD6{code}08102026{time}.pdf'))


def test_local_discovery_returns_token_free_verified_attachments(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    for code in ('SB21', 'SB22', 'SB35'):
        store.put_pdf(revision(code), code.encode())
    local = store.list_attachments({'SB22'})
    assert len(local) == 1
    assert local[0].metadata == revision().metadata
    assert local[0].attachment_id is None
    assert local[0].inline_data is None
    assert store.get_pdf(local[0]) == b'SB22'
    assert BillStorage(tmp_path, IDENTITY.model_copy(update={'port_code': 'INMBD7'})).list_attachments() == []


@pytest.mark.parametrize('corruption', ['pdf', 'json', 'metadata', 'sources', 'filename', 'missing_pdf'])
def test_local_discovery_reports_corruption_explicitly(tmp_path, corruption):
    store = BillStorage(tmp_path, IDENTITY)
    item = revision()
    store.put_pdf(item, b'original')
    path = store.path / 'processed' / f'{item.metadata.filename[:-4]}.json'
    pdf = store.path / 'pdfs' / item.metadata.filename
    if corruption == 'pdf':
        pdf.write_bytes(b'corrupt')
    elif corruption == 'json':
        path.write_text('{bad')
    elif corruption == 'filename':
        pdf.rename(pdf.with_name('unrecognized.pdf'))
    elif corruption == 'missing_pdf':
        pdf.unlink()
    else:
        record = json.loads(path.read_text())
        record[corruption] = [] if corruption == 'sources' else {}
        path.write_text(json.dumps(record))
    with pytest.raises(StorageError):
        store.list_attachments()


def test_pruning_keeps_newest_three_originals_and_matching_processed_siblings(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    for code in ('SB21', 'SB22', 'SB35'):
        older, newer = revision(code), revision(code, '1015')
        old_hash = store.put_pdf(older, b'older')
        store.put_layout(older.metadata, old_hash, DocumentLayout())
        store.put_processed(older.metadata, old_hash,
            ParsedDocument(metadata=older.metadata, content_hash=old_hash), '# Older')
        new_hash = store.put_pdf(newer, b'newer')
        store.put_layout(newer.metadata, new_hash, DocumentLayout())
        store.put_processed(newer.metadata, new_hash,
            ParsedDocument(metadata=newer.metadata, content_hash=new_hash), '# Newer')
        store.prune_revisions(newer.metadata, new_hash)
        assert store.get_pdf(older) is None
        assert store.get_pdf(newer) == b'newer'
    assert len(list((store.path / 'pdfs').glob('*.pdf'))) == 3
    assert len(list((store.path / 'processed').glob('*.json'))) == 3
    assert len(list((store.path / 'processed').glob('*.md'))) == 3
    assert all(item.metadata.document_at.hour == 10 for item in store.list_attachments())


def test_older_downloads_cannot_downgrade_retained_newest(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    newest = revision(time='1015')
    newest_hash = store.put_pdf(newest, b'newest')
    with pytest.raises(StorageError, match='newer'):
        store.put_pdf(revision(), b'older')
    store.prune_revisions(newest.metadata, newest_hash)
    assert len(store.list_attachments()) == 1
    assert store.get_pdf(newest) == b'newest'


def test_invalid_or_failed_replacement_never_prunes_existing_copy(tmp_path, monkeypatch):
    store = BillStorage(tmp_path, IDENTITY)
    old, new = revision(), revision(time='1015')
    old_hash = store.put_pdf(old, b'old')
    with pytest.raises(StorageError):
        store.prune_revisions(new.metadata, 'a' * 64)
    assert store.get_pdf(old) == b'old'
    actual_write = store._atomic_write
    monkeypatch.setattr(store, '_atomic_write', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(StorageError, match='disk full'):
        store.put_pdf(new, b'new')
    assert store.get_pdf(old) == b'old'
    monkeypatch.setattr(store, '_atomic_write', actual_write)
    new_hash = store.put_pdf(new, b'new')
    (store.path / 'pdfs' / new.metadata.filename).write_bytes(b'corrupt')
    with pytest.raises(StorageError, match='hash'):
        store.prune_revisions(new.metadata, new_hash)
    assert store.get_pdf(old) == b'old'
    with pytest.raises(StorageError, match='newer'):
        store.prune_revisions(old.metadata, old_hash)


def test_discover_local_resolves_missing_identity_from_existing_batch_only(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(revision(), b'local')
    for identity in (BillIdentity(sb_no='7546404'), IDENTITY.model_copy(update={'port_code': None}),
                     IDENTITY.model_copy(update={'sb_date': None})):
        found = BillStorage.discover_local(tmp_path, identity, {'SB22'})
        assert len(found) == 1
        assert found[0].metadata.identity.sb_date == IDENTITY.sb_date
        assert found[0].metadata.identity.port_code == IDENTITY.port_code
        assert store.get_pdf(found[0]) == b'local'
    assert len(list(tmp_path.iterdir())) == 1
    assert BillStorage.discover_local(tmp_path, BillIdentity(sb_no='9999999'), {'SB22'}) == []
    assert len(list(tmp_path.iterdir())) == 1


def test_discover_local_excludes_conflicting_known_identity_and_other_bills(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(revision(), b'local')
    assert BillStorage.discover_local(tmp_path, IDENTITY.model_copy(update={'port_code': 'INMBD7'}), {'SB22'}) == []
    assert BillStorage.discover_local(tmp_path, IDENTITY, {'SB35'}) == []
    other = tmp_path / '2026' / '999999907102026INMBD6' / 'pdfs'
    other.mkdir(parents=True)
    (other / 'malformed.pdf').write_bytes(b'bad')
    assert len(BillStorage.discover_local(tmp_path, IDENTITY, {'SB22'})) == 1


def test_discover_local_reports_corrupt_existing_batch(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(revision(), b'local')
    (store.path / 'pdfs' / revision().metadata.filename).unlink()
    with pytest.raises(StorageError, match='Missing retained PDF'):
        BillStorage.discover_local(tmp_path, BillIdentity(sb_no='7546404'), {'SB22'})


def test_newest_observation_survives_scoped_result_and_blocks_older_local_pdf(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    older, failed_newest = revision(), revision(time='1015')
    digest = store.put_pdf(older, b'old')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[older.metadata], selected_hashes={'SB22': digest}))
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[failed_newest.metadata], errors=['download failed']))
    unrelated = revision('SB21')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[unrelated.metadata],
                               fields={'exporter_name': FieldResult(state='missing')}))
    saved = json.loads((store.path / 'result.json').read_text())
    assert {m['code']: m['filename'] for m in saved['selected_sources']}['SB22'] == failed_newest.metadata.filename
    assert 'SB22' not in saved['selected_hashes']
    assert set(saved['fields']) == {'exporter_name'}
    with pytest.raises(StorageError, match='newest observed'):
        store.list_attachments({'SB22'})
    assert store.list_attachments({'SB35'}) == []
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[older.metadata], selected_hashes={'SB22': digest}))
    with pytest.raises(StorageError, match='newest observed'):
        store.list_attachments({'SB22'})
    new_hash = store.put_pdf(failed_newest, b'new')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[failed_newest.metadata], selected_hashes={'SB22': new_hash}))
    store.prune_revisions(failed_newest.metadata, new_hash)
    assert store.get_pdf(store.list_attachments({'SB22'})[0]) == b'new'


def test_result_only_observed_failure_is_discovered_and_never_silent_absence(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    newest = revision()
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[newest.metadata], errors=['download failed']))
    with pytest.raises(StorageError, match='newest observed'):
        BillStorage.discover_local(tmp_path, BillIdentity(sb_no='7546404'), {'SB22'})
    assert BillStorage.discover_local(tmp_path, BillIdentity(sb_no='7546404'), {'SB21'}) == []


def test_scoped_results_preserve_matching_retained_hash_without_business_value_merge(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    item = revision()
    digest = store.put_pdf(item, b'local')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[item.metadata], selected_hashes={'SB22': digest},
                               fields={'leo_date': FieldResult(state='blank')}))
    store.put_result(BillResult(identity=IDENTITY, fields={'exporter_name': FieldResult(state='missing')}))
    saved = json.loads((store.path / 'result.json').read_text())
    assert saved['selected_hashes'] == {'SB22': digest}
    assert 'leo_date' not in saved['fields']


def test_newer_retained_revision_survives_interruption_before_result_update(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    older, newer = revision(), revision(time='1015')
    old_hash = store.put_pdf(older, b'old')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[older.metadata], selected_hashes={'SB22': old_hash}))
    new_hash = store.put_pdf(newer, b'new')
    store.prune_revisions(newer.metadata, new_hash)
    local = store.list_attachments({'SB22'})
    assert len(local) == 1
    assert local[0].metadata == newer.metadata
    assert store.get_pdf(local[0]) == b'new'



def test_cross_year_document_uses_shipping_bill_year_and_local_discovery(tmp_path):
    identity = BillIdentity(sb_no='7546404', sb_date=date(2026, 12, 31), port_code='INMBD6')
    item = Attachment(message_id='january-leo', part_id='1',
        metadata=rules.parse_filename('754640431122026INMBD6SB22010120270915.pdf'))
    store = BillStorage(tmp_path, identity)
    store.put_pdf(item, b'january')
    assert store.path == tmp_path / '2026' / '754640431122026INMBD6'
    assert BillStorage.discover_local(tmp_path, identity, {'SB22'})[0].metadata == item.metadata
    assert BillStorage.discover_local(tmp_path, BillIdentity(sb_no='7546404'), {'SB22'})[0].metadata == item.metadata
    assert not (tmp_path / '2027').exists()


def test_get_result_returns_typed_validated_result_without_reusing_values(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    assert store.get_result() is None
    item = revision()
    digest = store.put_pdf(item, b'local')
    result = BillResult(identity=IDENTITY, selected_sources=[item.metadata], selected_hashes={'SB22': digest},
                        fields={'leo_date': FieldResult(state='blank')})
    store.put_result(result)
    assert store.get_result() == result
    (store.path / 'pdfs' / item.metadata.filename).write_bytes(b'corrupt')
    with pytest.raises(StorageError, match='hash'):
        store.get_result()


@pytest.mark.parametrize('payload', ['{bad', '{}', '{"identity":{"sb_no":"9999999"}}'])
def test_get_result_rejects_corrupt_or_conflicting_result(tmp_path, payload):
    store = BillStorage(tmp_path, IDENTITY)
    (store.path / 'result.json').write_text(payload)
    with pytest.raises(StorageError):
        store.get_result()


def test_known_newer_failed_observation_blocks_older_online_retention_and_pruning(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    older, failed_newest = revision(), revision(time='1015')
    digest = store.put_pdf(older, b'old')
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[older.metadata], selected_hashes={'SB22': digest}))
    store.put_result(BillResult(identity=IDENTITY, selected_sources=[failed_newest.metadata], errors=['download failed']))
    with pytest.raises(StorageError, match='newer'):
        store.prune_revisions(older.metadata, digest)
    with pytest.raises(StorageError, match='newer'):
        store.put_pdf(older, b'old')
    assert store.get_pdf(older) == b'old'
    assert len(list((store.path / 'pdfs').glob('*.pdf'))) == 1
