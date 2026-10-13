"""Private atomic artifacts and version-aware reuse, isolated by bill identity."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from pydantic import ValidationError

from . import rules
from .models import Attachment, BillIdentity, BillResult, DocumentLayout, ParsedDocument

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class StorageError(RuntimeError):
    """Persistence failed; the caller must not treat evidence as safely stored."""


class BillStorage:
    def __init__(self, root: Path, identity: BillIdentity):
        self.identity = identity.model_copy(update={'document_date': None})
        date_part = identity.sb_date.strftime('%d%m%Y') if identity.sb_date else 'unknown'
        port_part = identity.port_code or 'unknown'
        self.path = Path(root) / identity.sb_no / f'{date_part}-{port_part}'
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

    def _manifest(self):
        payload = self._read_json(self.path / 'manifest.json')
        if isinstance(payload, dict) and payload.get('identity') == self.identity.model_dump(mode='json'):
            if isinstance(payload.get('attachments'), dict):
                return payload
        return {'identity': self.identity.model_dump(mode='json'), 'attachments': {}, 'selected_sources': []}

    @staticmethod
    def _hash(raw: bytes):
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _validate_hash(content_hash: str):
        if not re.fullmatch(r'[0-9a-f]{64}', content_hash):
            raise StorageError('Invalid content hash for artifact path')

    def _processed_path(self, content_hash: str):
        self._validate_hash(content_hash)
        path = self.path / 'processed' / content_hash
        return path

    def get_pdf(self, attachment: Attachment) -> bytes | None:
        with self._locked():
            entry = self._manifest()['attachments'].get(attachment.key)
            if not isinstance(entry, dict) or not isinstance(entry.get('attachment'), dict):
                return None
            stored = entry['attachment']
            current = attachment.model_dump(mode='json')
            if any(stored.get(key) != current[key] for key in ('message_id', 'part_id', 'metadata')):
                return None
            filename = entry.get('stored_filename')
            if not isinstance(filename, str) or Path(filename).name != filename:
                return None
            try:
                raw = (self.path / 'pdfs' / filename).read_bytes()
            except FileNotFoundError:
                return None
            if self._hash(raw) != entry.get('content_hash'):
                return None
            if attachment.inline_data is not None:
                try:
                    encoded = attachment.inline_data
                    inline = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
                except ValueError:
                    return None
                if self._hash(inline) != entry['content_hash']:
                    return None
            return raw

    def put_pdf(self, attachment: Attachment, raw: bytes) -> str:
        filename = attachment.metadata.filename
        if Path(filename).name != filename or not filename:
            raise StorageError('Unsafe attachment filename')
        if not rules.matches_identity(self.identity, attachment.metadata.identity):
            raise StorageError('Attachment identity does not match bill storage')
        content_hash = self._hash(raw)
        with self._locked():
            manifest = self._manifest()
            destination = self.path / 'pdfs' / filename
            if destination.exists() and self._hash(destination.read_bytes()) != content_hash:
                destination = destination.with_name(f'{destination.stem}--{content_hash}{destination.suffix}')
            if not destination.exists() or self._hash(destination.read_bytes()) != content_hash:
                self._atomic_write(destination, raw)
            manifest['attachments'][attachment.key] = {
                'attachment': attachment.model_dump(mode='json'), 'content_hash': content_hash,
                'stored_filename': destination.name,
            }
            self._write_json(self.path / 'manifest.json', manifest)
        return content_hash

    def get_layout(self, content_hash: str) -> DocumentLayout | None:
        with self._locked():
            envelope = self._read_json(self._processed_path(content_hash) / 'layout.json')
            if not self._valid_envelope(envelope, content_hash, parsed=False):
                return None
            try:
                return DocumentLayout.model_validate(envelope['value'])
            except (ValidationError, KeyError, TypeError):
                return None

    def put_layout(self, content_hash: str, layout: DocumentLayout):
        with self._locked():
            self._write_json(self._processed_path(content_hash) / 'layout.json',
                             self._envelope(content_hash, layout, parsed=False))
            self._record_processed(content_hash, parsed=False)

    def get_parsed(self, content_hash: str) -> ParsedDocument | None:
        with self._locked():
            envelope = self._read_json(self._processed_path(content_hash) / 'parsed.json')
            if not self._valid_envelope(envelope, content_hash, parsed=True):
                return None
            try:
                parsed = ParsedDocument.model_validate(envelope['value'])
                if parsed.content_hash != content_hash or any(f.state == 'error' for f in parsed.fields.values()):
                    return None
                if not rules.matches_identity(self.identity, parsed.metadata.identity):
                    return None
                return parsed
            except (ValidationError, KeyError, TypeError):
                return None

    def put_processed(self, content_hash: str, parsed: ParsedDocument, markdown: str):
        if parsed.content_hash != content_hash or not rules.matches_identity(self.identity, parsed.metadata.identity):
            raise StorageError('Parsed document hash/identity does not match storage')
        with self._locked():
            path = self._processed_path(content_hash)
            self._atomic_write(path / 'document.md', markdown.encode())
            self._write_json(path / 'parsed.json', self._envelope(content_hash, parsed, parsed=True))
            self._record_processed(content_hash, parsed=True)

    def _record_processed(self, content_hash: str, parsed: bool):
        manifest = self._manifest()
        records = manifest.get('processed')
        if not isinstance(records, dict):
            records = {}
            manifest['processed'] = records
        records[content_hash] = {'versions': self._versions(parsed)}
        self._write_json(self.path / 'manifest.json', manifest)

    def put_result(self, result: BillResult):
        if result.identity.model_copy(update={'document_date': None}) != self.identity:
            raise StorageError('Bill result identity does not match storage')
        with self._locked():
            self._write_json(self.path / 'result.json', result.model_dump(mode='json'))
            manifest = self._manifest()
            manifest['selected_sources'] = [m.model_dump(mode='json') for m in result.selected_sources]
            manifest['selected_hashes'] = result.selected_hashes
            manifest['versions'] = self._versions(parsed=True)
            self._write_json(self.path / 'manifest.json', manifest)

    @staticmethod
    def _versions(parsed: bool):
        try:
            versions = {'layout': rules.LAYOUT_VERSION, 'rules': rules.RULES_VERSION,
                        'anydoc': package_version('firecrawl-anydoc'), 'pymupdf': package_version('PyMuPDF')}
        except PackageNotFoundError as exc:
            raise StorageError(f'Cannot identify installed converter version: {exc}') from exc
        if parsed:
            versions.update(parser=rules.PARSER_VERSION)
        return versions

    def _envelope(self, content_hash, value, parsed):
        return {'content_hash': content_hash, 'versions': self._versions(parsed), 'value': value.model_dump(mode='json')}

    def _valid_envelope(self, envelope, content_hash, parsed):
        return (isinstance(envelope, dict) and envelope.get('content_hash') == content_hash
                and envelope.get('versions') == self._versions(parsed))
