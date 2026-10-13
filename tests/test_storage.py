"""Private, versioned local artifacts independent of business documents."""
import json
import stat
from datetime import date, datetime
from importlib.metadata import version

import pytest

from daily_report.models import (
    Attachment,
    BillIdentity,
    BillResult,
    DocumentLayout,
    DocumentMetadata,
    FieldResult,
    ParsedDocument,
)
from daily_report.storage import BillStorage, StorageError

IDENTITY = BillIdentity(sb_no='7546404', sb_date=date(2026, 10, 7), port_code='INMBD6')


def attachment(message='one'):
    return Attachment(message_id=message, attachment_id='blob', metadata=DocumentMetadata(
        identity=IDENTITY, code='SB22', document_at=datetime(2026, 10, 8, 9, 15),
        filename='754640407102026INMBD6SB22081020260915.pdf'))


def test_pdf_collision_retains_original_bytes_and_provenance(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    first, second = attachment(), attachment('two')
    hash1 = store.put_pdf(first, b'first')
    hash2 = store.put_pdf(second, b'second')
    assert hash1 != hash2
    assert store.get_pdf(first) == b'first'
    assert store.get_pdf(second) == b'second'
    files = list(tmp_path.rglob('*.pdf'))
    assert len(files) == 2
    assert first.metadata.filename in {p.name for p in files}
    assert any(hash2 in p.name for p in files)
    manifest = json.loads(next(tmp_path.rglob('manifest.json')).read_text())
    assert manifest['identity']['sb_no'] == IDENTITY.sb_no
    assert len(manifest['attachments']) == 2
    assert store.get_pdf(attachment('never-seen')) is None


def test_same_sb_different_identity_and_unknown_identity_are_isolated(tmp_path):
    identities = [IDENTITY, IDENTITY.model_copy(update={'sb_date': date(2026, 10, 9)}),
                  IDENTITY.model_copy(update={'port_code': 'INMBD7'}), BillIdentity(sb_no='7546404')]
    stores = [BillStorage(tmp_path, identity) for identity in identities]
    stores[0].put_pdf(attachment(), b'first')
    assert len({store.path for store in stores}) == 4
    assert all(store.get_pdf(attachment()) is None for store in stores[1:])
    for store, identity in zip(stores, identities, strict=True):
        store.put_result(BillResult(identity=identity))
    assert len(list(tmp_path.rglob('result.json'))) == 4


def test_pdf_reuse_validates_hash_and_corrupt_json_is_cache_miss(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    store.put_pdf(attachment(), b'first')
    pdf = next(tmp_path.rglob('*.pdf'))
    pdf.write_bytes(b'corrupt')
    assert store.get_pdf(attachment()) is None
    (store.path / 'manifest.json').write_text('{bad')
    assert store.get_pdf(attachment()) is None


def test_version_invalidation_and_typed_artifacts(tmp_path, monkeypatch):
    from daily_report import rules
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    layout = DocumentLayout()
    parsed = ParsedDocument(metadata=attachment().metadata, content_hash=hash_, fields={'leo_date': FieldResult(state='blank')})
    store.put_layout(hash_, layout)
    store.put_processed(hash_, parsed, '# Inspection')
    assert store.get_layout(hash_) == layout
    assert store.get_parsed(hash_) == parsed
    assert (store.path / 'processed' / hash_ / 'document.md').read_text() == '# Inspection'
    monkeypatch.setattr(rules, 'PARSER_VERSION', 'changed')
    assert store.get_layout(hash_) == layout
    assert store.get_parsed(hash_) is None
    monkeypatch.setattr(rules, 'LAYOUT_VERSION', 'changed')
    assert store.get_layout(hash_) is None


def test_runtime_failures_are_not_reused(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    parsed = ParsedDocument(metadata=attachment().metadata, content_hash=hash_, fields={'leo_date': FieldResult(state='error', reason='conversion failed')})
    store.put_processed(hash_, parsed, '')
    assert store.get_parsed(hash_) is None


def test_permissions_and_storage_failures(tmp_path, monkeypatch):
    store = BillStorage(tmp_path / 'data', IDENTITY)
    store.put_pdf(attachment(), b'first')
    store.put_result(BillResult(identity=IDENTITY))
    for path in (tmp_path / 'data').rglob('*'):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    monkeypatch.setattr(store, '_atomic_write', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(StorageError, match='disk full'):
        store.put_result(BillResult(identity=IDENTITY))


def test_two_storage_instances_merge_manifest_under_lock(tmp_path):
    first, second = BillStorage(tmp_path, IDENTITY), BillStorage(tmp_path, IDENTITY)
    first.put_pdf(attachment('first'), b'one')
    second.put_pdf(attachment('second'), b'two')
    assert first.get_pdf(attachment('second')) == b'two'
    assert second.get_pdf(attachment('first')) == b'one'


def test_concurrent_instances_preserve_all_attachment_records(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def write(index):
        BillStorage(tmp_path, IDENTITY).put_pdf(attachment(str(index)), str(index).encode())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(16)))
    store = BillStorage(tmp_path, IDENTITY)
    assert all(store.get_pdf(attachment(str(i))) == str(i).encode() for i in range(16))


def test_processing_manifest_records_versions_before_bill_result(tmp_path):
    from daily_report import rules
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    store.put_layout(hash_, DocumentLayout())
    parsed = ParsedDocument(metadata=attachment().metadata, content_hash=hash_)
    store.put_processed(hash_, parsed, '')
    manifest = json.loads((store.path / 'manifest.json').read_text())
    assert manifest['processed'][hash_]['versions'] == {
        'layout': rules.LAYOUT_VERSION, 'parser': rules.PARSER_VERSION, 'rules': rules.RULES_VERSION,
        'anydoc': version('firecrawl-anydoc'), 'pymupdf': version('PyMuPDF')}


def test_corrupt_optional_manifest_processing_entry_is_rebuilt(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    manifest_path = store.path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['processed'] = []
    manifest_path.write_text(json.dumps(manifest))
    store.put_layout(hash_, DocumentLayout())
    assert store.get_layout(hash_) == DocumentLayout()
    assert store.get_pdf(attachment()) == b'first'


def test_layout_invalidates_on_rules_and_converter_package_versions(tmp_path, monkeypatch):
    from daily_report import rules, storage
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    store.put_layout(hash_, DocumentLayout())
    monkeypatch.setattr(rules, 'RULES_VERSION', 'new-pages-policy')
    assert store.get_layout(hash_) is None
    store.put_layout(hash_, DocumentLayout())
    actual_version = storage.package_version
    monkeypatch.setattr(storage, 'package_version',
        lambda name: 'new-pymupdf' if name == 'PyMuPDF' else actual_version(name))
    assert store.get_layout(hash_) is None


def test_selected_blank_source_hash_is_persisted_with_metadata(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    hash_ = store.put_pdf(attachment(), b'first')
    result = BillResult(identity=IDENTITY, selected_sources=[attachment().metadata],
        selected_hashes={'SB22': hash_}, fields={'leo_date': FieldResult(state='blank')})
    store.put_result(result)
    manifest = json.loads((store.path / 'manifest.json').read_text())
    assert manifest['selected_hashes'] == {'SB22': hash_}
    assert manifest['selected_sources'][0]['filename'] == attachment().metadata.filename
    assert json.loads((store.path / 'result.json').read_text())['selected_hashes'] == {'SB22': hash_}


def test_rotating_download_ids_reuse_immutable_message_part_and_metadata(tmp_path):
    store = BillStorage(tmp_path, IDENTITY)
    original = attachment().model_copy(update={'part_id': '1.0', 'attachment_id': 'download-v1'})
    rotated = original.model_copy(update={'attachment_id': 'download-v2'})
    store.put_pdf(original, b'first')
    assert store.get_pdf(rotated) == b'first'
    different_part = rotated.model_copy(update={'part_id': '1.1'})
    assert store.get_pdf(different_part) is None
    store.put_pdf(different_part, b'second')
    assert store.get_pdf(original) == b'first'
    assert store.get_pdf(different_part) == b'second'


def test_changed_current_inline_bytes_invalidate_cached_pdf(tmp_path):
    import base64
    store = BillStorage(tmp_path, IDENTITY)
    original = attachment().model_copy(update={'part_id': '1.0', 'attachment_id': None,
        'inline_data': base64.urlsafe_b64encode(b'first').decode()})
    store.put_pdf(original, b'first')
    assert store.get_pdf(original) == b'first'
    changed = original.model_copy(update={'inline_data': base64.urlsafe_b64encode(b'changed').decode()})
    assert store.get_pdf(changed) is None
