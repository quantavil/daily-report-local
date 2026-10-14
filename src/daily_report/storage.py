"""Private atomic artifacts and version-aware reuse, isolated by bill identity."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from pydantic import ValidationError

from . import rules
from .models import Attachment, BillIdentity, BillResult, DocumentLayout, DocumentMetadata, ParsedDocument

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class StorageError(RuntimeError):
    """Persistence failed; the caller must not treat evidence as safely stored."""


class BillStorage:
    def __init__(self, root: Path, identity: BillIdentity):
        self.identity = identity.model_copy(update={'document_date': None})
        self.path = Path(root) / rules.storage_year(identity) / rules.batch_name(identity)
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        try:
            for directory in (Path(root), self.path.parent, self.path):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
        except OSError as exc:
            raise StorageError(f'Cannot initialize bill storage: {exc}') from exc

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            try:
                fd = os.open(self.path / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError as exc:
                raise StorageError(f'Bill storage failed: {exc}') from exc

    def _atomic_write(self, path: Path, data: bytes):
        relative = path.parent.relative_to(self.path)
        directory = self.path
        for part in relative.parts:
            directory = directory / part
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_json(self, path: Path, payload):
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode())

    @staticmethod
    def _read_json(path: Path):
        try:
            return json.loads(path.read_bytes())
        except (FileNotFoundError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _hash(raw: bytes):
        return hashlib.sha256(raw).hexdigest()

    def _document_path(self, metadata: DocumentMetadata) -> Path:
        try:
            canonical = rules.parse_filename(metadata.filename)
        except ValueError as exc:
            raise StorageError('Unsafe or noncanonical attachment filename') from exc
        normalized = metadata.model_copy(update={
            'identity': metadata.identity.model_copy(update={'document_date': metadata.document_at.date()})})
        if canonical != normalized:
            raise StorageError('Filename metadata does not match document metadata')
        if not rules.matches_identity(self.identity, metadata.identity):
            raise StorageError('Document identity does not match bill storage')
        return self.path / 'processed' / f'{Path(metadata.filename).stem}.json'

    def _record(self, metadata: DocumentMetadata, content_hash: str | None = None):
        record = self._read_json(self._document_path(metadata))
        if not isinstance(record, dict) or record.get('metadata') != metadata.model_dump(mode='json'):
            return None
        if content_hash is not None and record.get('content_hash') != content_hash:
            return None
        return record

    @staticmethod
    def _source(attachment: Attachment):
        return {'message_id': attachment.message_id, 'part_id': attachment.part_id}

    def get_pdf(self, attachment: Attachment) -> bytes | None:
        with self._locked():
            try:
                record = self._record(attachment.metadata)
            except StorageError:
                return None
            if record is None or self._source(attachment) not in record.get('sources', []):
                return None
            try:
                raw = (self.path / 'pdfs' / attachment.metadata.filename).read_bytes()
            except FileNotFoundError:
                return None
            if self._hash(raw) != record.get('content_hash'):
                return None
            if attachment.inline_data is not None:
                try:
                    encoded = attachment.inline_data
                    inline = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
                except ValueError:
                    return None
                if self._hash(inline) != record['content_hash']:
                    return None
            return raw

    def _saved_metadata(self):
        """Inventory canonical PDF names, without trusting cached business values."""
        items = []
        for path in sorted((self.path / 'pdfs').glob('*.pdf')):
            try:
                metadata = rules.parse_filename(path.name)
            except ValueError as exc:
                raise StorageError(f'Malformed retained PDF filename: {path.name}') from exc
            if not rules.matches_identity(self.identity, metadata.identity):
                raise StorageError(f'Retained PDF identity conflicts with batch: {path.name}')
            items.append(metadata)
        return items

    def _observed_sources(self, payload=None):
        if payload is None:
            path = self.path / 'result.json'
            payload = self._read_json(path)
            if payload is None and not path.exists():
                return {}
        if not isinstance(payload, dict) or not isinstance(payload.get('selected_sources'), list):
            raise StorageError('Corrupt saved batch source metadata')
        newest = {}
        try:
            for raw in payload['selected_sources']:
                metadata = DocumentMetadata.model_validate(raw)
                self._document_path(metadata)
                previous = newest.get(metadata.code)
                if previous is None or metadata.document_at > previous.document_at:
                    newest[metadata.code] = metadata
        except (ValidationError, ValueError, TypeError) as exc:
            raise StorageError('Corrupt saved batch source metadata') from exc
        return newest

    def list_attachments(self, codes: set[str] | None = None) -> list[Attachment]:
        """Return local source references; corrupted evidence is an explicit error."""
        with self._locked():
            metadata_items = self._saved_metadata()
            filenames = {item.filename for item in metadata_items}
            for metadata in self._observed_sources().values():
                if codes is not None and metadata.code not in codes:
                    continue
                if not any(item.code == metadata.code and item.document_at >= metadata.document_at
                           for item in metadata_items):
                    raise StorageError(f'Missing newest observed source: {metadata.filename}')
            for path in sorted((self.path / 'processed').glob('*.json')):
                try:
                    metadata = rules.parse_filename(f'{path.stem}.pdf')
                except ValueError as exc:
                    raise StorageError(f'Malformed processed document filename: {path.name}') from exc
                if codes is not None and metadata.code not in codes:
                    continue
                if metadata.filename not in filenames:
                    raise StorageError(f'Missing retained PDF: {metadata.filename}')
            attachments = []
            for metadata in metadata_items:
                if codes is not None and metadata.code not in codes:
                    continue
                record = self._record(metadata)
                if record is None:
                    raise StorageError(f'Missing or corrupt document metadata: {metadata.filename}')
                sources = record.get('sources')
                if (not isinstance(sources, list) or not sources
                        or any(not isinstance(source, dict) or not isinstance(source.get('message_id'), str)
                               or not source['message_id'] or (source.get('part_id') is not None
                               and not isinstance(source['part_id'], str)) for source in sources)):
                    raise StorageError(f'Missing or corrupt source provenance: {metadata.filename}')
                self._retained_record(metadata, record.get('content_hash'))
                attachments.append(Attachment(metadata=metadata, message_id=sources[0]['message_id'],
                                              part_id=sources[0].get('part_id')))
            return attachments

    @classmethod
    def discover_local(cls, root: Path, identity: BillIdentity, codes: set[str]) -> list[Attachment]:
        """Inspect existing matching SB batches without creating pending folders."""
        attachments = []
        prefix = identity.sb_no + (identity.sb_date.strftime('%d%m%Y') if identity.sb_date else '')
        root = Path(root)
        if identity.sb_date:
            years = [root / rules.storage_year(identity)]
        else:
            years = sorted(root.iterdir()) if root.exists() else []
        batches = sorted(batch for year in years if year.is_dir() for batch in year.glob(f'{prefix}*'))
        for batch in batches:
            if not batch.is_dir():
                continue
            if identity.sb_date and identity.port_code and batch.name != rules.batch_name(identity):
                continue
            artifacts = sorted((batch / 'pdfs').glob('*.pdf'))
            if not artifacts:
                artifacts = sorted((batch / 'processed').glob('*.json'))
            if artifacts:
                filename = artifacts[0].name if artifacts[0].suffix == '.pdf' else f'{artifacts[0].stem}.pdf'
                try:
                    metadata = rules.parse_filename(filename)
                except ValueError as exc:
                    raise StorageError(f'Malformed local batch artifact: {artifacts[0].name}') from exc
            else:
                result_path = batch / 'result.json'
                if not result_path.exists():
                    continue
                result = cls._read_json(result_path)
                if not isinstance(result, dict) or not isinstance(result.get('selected_sources'), list):
                    raise StorageError(f'Corrupt saved batch source metadata: {batch.name}')
                if not result['selected_sources']:
                    continue
                try:
                    metadata = DocumentMetadata.model_validate(result['selected_sources'][0])
                except ValidationError as exc:
                    raise StorageError(f'Corrupt saved batch source metadata: {batch.name}') from exc
            if batch.name != rules.batch_name(metadata.identity) or batch.parent.name != rules.storage_year(metadata.identity):
                raise StorageError(f'Local folder does not match retained bill identity: {batch.name}')
            if not rules.matches_identity(identity.model_copy(update={'document_date': None}), metadata.identity):
                continue
            attachments.extend(item for item in cls(root, metadata.identity).list_attachments(codes)
                               if rules.matches_identity(identity, item.metadata.identity))
        return attachments

    def prune_revisions(self, metadata: DocumentMetadata, content_hash: str):
        """Remove superseded siblings only after validating their replacement."""
        with self._locked():
            self._retained_record(metadata, content_hash)
            observed = self._observed_sources().get(metadata.code)
            if observed is not None and observed.document_at > metadata.document_at:
                raise StorageError('Cannot retain older revision after a newer observed candidate')
            items = [item for item in self._saved_metadata() if item.code == metadata.code]
            if any(item.document_at > metadata.document_at for item in items):
                raise StorageError('Cannot retain older revision while newer canonical PDF exists')
            for item in items:
                if item.document_at >= metadata.document_at:
                    continue
                pdf = self.path / 'pdfs' / item.filename
                record = self._document_path(item)
                for path in (pdf, record, record.with_suffix('.md')):
                    path.unlink(missing_ok=True)

    def put_pdf(self, attachment: Attachment, raw: bytes) -> str:
        path = self._document_path(attachment.metadata)
        content_hash = self._hash(raw)
        with self._locked():
            observed = self._observed_sources().get(attachment.metadata.code)
            if observed is not None and observed.document_at > attachment.metadata.document_at:
                raise StorageError('Cannot retain older revision after a newer observed candidate')
            if any(item.code == attachment.metadata.code and item.document_at > attachment.metadata.document_at
                   for item in self._saved_metadata()):
                raise StorageError('Cannot retain older revision while newer canonical PDF exists')
            destination = self.path / 'pdfs' / attachment.metadata.filename
            record = self._record(attachment.metadata, content_hash)
            if destination.exists() and self._hash(destination.read_bytes()) != content_hash:
                if record is None:
                    raise StorageError('Same canonical filename has different content; retained PDF preserved')
                # A matching recorded hash identifies a fresh download as repair.
                self._atomic_write(destination, raw)
            elif not destination.exists():
                self._atomic_write(destination, raw)
            if record is None:
                record = {'metadata': attachment.metadata.model_dump(mode='json'),
                          'content_hash': content_hash, 'sources': []}
            if not isinstance(record.get('sources'), list):
                record['sources'] = []
            source = self._source(attachment)
            if source not in record['sources']:
                record['sources'].append(source)
            self._write_json(path, record)
        return content_hash

    def get_layout(self, metadata: DocumentMetadata, content_hash: str) -> DocumentLayout | None:
        with self._locked():
            record = self._record(metadata, content_hash)
            if record is None or record.get('layout_versions') != self._layout_versions():
                return None
            try:
                return DocumentLayout.model_validate(record['layout'])
            except (ValidationError, KeyError, TypeError):
                return None

    def _retained_record(self, metadata: DocumentMetadata, content_hash: str):
        record = self._record(metadata, content_hash)
        if record is None:
            raise StorageError('Processing requires matching retained document')
        try:
            raw = (self.path / 'pdfs' / metadata.filename).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError('Processing requires retained PDF') from exc
        if self._hash(raw) != content_hash:
            raise StorageError('Retained PDF hash does not match processing output')
        return record

    def put_layout(self, metadata: DocumentMetadata, content_hash: str, layout: DocumentLayout):
        with self._locked():
            record = self._retained_record(metadata, content_hash)
            record.update(layout=layout.model_dump(mode='json'), layout_versions=self._layout_versions())
            record.pop('fields', None)
            record.pop('containers', None)
            record.pop('parser_version', None)
            self._write_json(self._document_path(metadata), record)

    def put_processed(self, metadata: DocumentMetadata, content_hash: str, parsed: ParsedDocument, markdown: str):
        if parsed.content_hash != content_hash or parsed.metadata != metadata:
            raise StorageError('Parsed document hash/metadata does not match storage')
        with self._locked():
            record = self._retained_record(metadata, content_hash)
            record.update(fields={name: field.model_dump(mode='json') for name, field in parsed.fields.items()},
                          containers=[container.model_dump(mode='json') for container in parsed.containers],
                          parser_version=rules.PARSER_VERSION)
            path = self._document_path(metadata)
            self._atomic_write(path.with_suffix('.md'), markdown.encode())
            self._write_json(path, record)

    def get_result(self) -> BillResult | None:
        """Validate saved run evidence for policy checks; extraction never reuses it."""
        with self._locked():
            path = self.path / 'result.json'
            if not path.exists():
                return None
            try:
                result = BillResult.model_validate(self._read_json(path))
            except (ValidationError, TypeError) as exc:
                raise StorageError('Corrupt saved bill result') from exc
            if not rules.matches_identity(result.identity.model_copy(update={'document_date': None}), self.identity):
                raise StorageError('Saved bill result identity does not match storage')
            sources = self._observed_sources(result.model_dump(mode='json'))
            for code, digest in result.selected_hashes.items():
                metadata = sources.get(code)
                if metadata is None:
                    raise StorageError('Saved bill result hash has no source metadata')
                self._retained_record(metadata, digest)
            return result

    def put_result(self, result: BillResult):
        if not rules.matches_identity(result.identity.model_copy(update={'document_date': None}), self.identity):
            raise StorageError('Bill result identity does not match storage')
        with self._locked():
            payload = result.model_dump(mode='json')
            previous_payload = self._read_json(self.path / 'result.json')
            previous = self._observed_sources()
            current = self._observed_sources(payload)
            selected = dict(previous)
            for code, metadata in current.items():
                if code not in selected or metadata.document_at >= selected[code].document_at:
                    selected[code] = metadata
            hashes = {}
            for code, metadata in selected.items():
                if current.get(code) == metadata and code in result.selected_hashes:
                    hashes[code] = result.selected_hashes[code]
                elif previous.get(code) == metadata and isinstance(previous_payload, dict):
                    previous_hashes = previous_payload.get('selected_hashes', {})
                    if not isinstance(previous_hashes, dict):
                        raise StorageError('Corrupt saved batch source hashes')
                    digest = previous_hashes.get(code)
                    if digest:
                        hashes[code] = digest
            for code, digest in list(hashes.items()):
                try:
                    self._retained_record(selected[code], digest)
                except StorageError:
                    del hashes[code]
            payload['selected_sources'] = [selected[code].model_dump(mode='json') for code in sorted(selected)]
            payload['selected_hashes'] = hashes
            self._write_json(self.path / 'result.json', payload)

    @staticmethod
    def _layout_versions():
        try:
            return {'layout': rules.LAYOUT_VERSION, 'rules': rules.RULES_VERSION,
                    'pymupdf': package_version('PyMuPDF')}
        except PackageNotFoundError as exc:
            raise StorageError(f'Cannot identify installed native converter version: {exc}') from exc
