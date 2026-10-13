"""Read-only Gmail discovery and byte downloads; no field extraction."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from email.utils import getaddresses
from pathlib import Path

from . import rules
from .models import Attachment, BillIdentity

_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
_AUTH_LOCK = threading.RLock()


class GmailError(RuntimeError):
    """A service/authentication failure, distinct from an empty search."""


class GmailClient:
    def __init__(self, credentials_file: Path, token_file: Path, service=None):
        self.credentials_file = Path(credentials_file)
        self.token_file = Path(token_file)
        self._injected_service = service
        self._local = threading.local()
        self._credentials = None

    def authenticate(self):
        """Load/refresh credentials once, with refresh and token writes serialized."""
        if self._injected_service is not None:
            return
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow

            class LockedCredentials(Credentials):
                # The transport may trigger refresh during execute(), outside authenticate().
                def refresh(self, request):
                    with _AUTH_LOCK:
                        return super().refresh(request)

            with _AUTH_LOCK:
                if self._credentials is not None and self._credentials.valid:
                    return
                credentials = self._credentials
                if credentials is None and self.token_file.exists():
                    credentials = LockedCredentials.from_authorized_user_file(str(self.token_file), _SCOPES)
                if not credentials or not credentials.valid:
                    if credentials and credentials.expired and credentials.refresh_token:
                        credentials.refresh(Request())
                    else:
                        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), _SCOPES)
                        credentials = flow.run_local_server(port=0)
                        credentials = LockedCredentials.from_authorized_user_info(
                            json.loads(credentials.to_json()), _SCOPES)
                    self.token_file.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(dir=self.token_file.parent, prefix='.token-')
                    try:
                        with os.fdopen(fd, 'w') as stream:
                            stream.write(credentials.to_json())
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, self.token_file)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                self._credentials = credentials
        except Exception as exc:
            raise GmailError(f'Gmail authentication failed: {exc}') from exc

    def _service(self):
        if self._injected_service is not None:
            return self._injected_service
        self.authenticate()
        if not hasattr(self._local, 'service'):
            from googleapiclient.discovery import build
            self._local.service = build('gmail', 'v1', credentials=self._credentials, cache_discovery=False)
        return self._local.service

    def discover(self, identity: BillIdentity, codes: set[str]) -> list[Attachment]:
        """Refresh message metadata, paginate, and validate every attachment filename."""
        if not codes:
            return []
        prefixes = [rules.filename_prefix(identity, code) for code in sorted(codes)]
        # Gmail filename search matches tokens, not prefixes within canonical names.
        # Use known indexed identity tokens; exact prefixes remain a local check.
        query = f'from:{rules.SENDER} has:attachment -in:spam -in:trash'
        identity_terms = rules.gmail_identity_terms(identity)
        if identity_terms:
            query += ' {' + ' '.join(f'"{term}"' for term in identity_terms) + '}'
        try:
            messages = self._service().users().messages()
            found: dict[str, Attachment] = {}
            seen_messages: set[str] = set()
            page_token = None
            seen_tokens = set()
            while True:
                arguments = {'userId': 'me', 'q': query, 'includeSpamTrash': False, 'maxResults': 100}
                if page_token:
                    arguments['pageToken'] = page_token
                response = messages.list(**arguments).execute()
                for stub in response.get('messages', []):
                    message_id = stub['id']
                    if message_id in seen_messages:
                        continue
                    seen_messages.add(message_id)
                    message = messages.get(userId='me', id=message_id, format='full').execute()
                    if {'SPAM', 'TRASH'} & set(message.get('labelIds', [])):
                        continue
                    payload = message.get('payload', {})
                    headers = payload.get('headers', [])
                    forwarded = {'x-forwarded-for', 'x-forwarded-to', 'resent-from', 'resent-to'}
                    if any(h.get('name', '').lower() in forwarded for h in headers):
                        continue
                    senders = [h.get('value', '') for h in headers
                               if h.get('name', '').lower() == 'from']
                    if len(senders) != 1:
                        continue
                    mailboxes = getaddresses(senders)
                    if len(mailboxes) != 1 or mailboxes[0][1].lower() != rules.SENDER.lower():
                        continue
                    for ordinal, part in enumerate(self._parts(payload)):
                        filename = part.get('filename', '')
                        if not filename or not any(filename.startswith(prefix) for prefix in prefixes):
                            continue
                        try:
                            metadata = rules.parse_filename(filename)
                        except ValueError:
                            continue
                        if metadata.code not in codes or not rules.matches_identity(identity, metadata.identity):
                            continue
                        if identity.document_date and identity.document_date != metadata.document_at.date():
                            continue
                        body = part.get('body', {})
                        if not body.get('attachmentId') and 'data' not in body:
                            continue
                        attachment = Attachment(metadata=metadata, message_id=message_id,
                                                attachment_id=body.get('attachmentId'),
                                                inline_data=body.get('data'),
                                                part_id=part.get('partId') or f'ordinal:{ordinal}')
                        found[attachment.key] = attachment
                page_token = response.get('nextPageToken')
                if not page_token:
                    break
                if page_token in seen_tokens:
                    raise GmailError('Gmail discovery returned a repeated pagination token')
                seen_tokens.add(page_token)
            return list(found.values())
        except GmailError:
            raise
        except Exception as exc:
            raise GmailError(f'Gmail discovery failed: {exc}') from exc

    @staticmethod
    def _parts(payload):
        yield payload
        for part in payload.get('parts', []):
            yield from GmailClient._parts(part)

    def download(self, attachment: Attachment) -> bytes:
        try:
            encoded = attachment.inline_data
            if encoded is None:
                if not attachment.attachment_id:
                    raise ValueError('Attachment has neither inline bytes nor an attachment ID')
                response = self._service().users().messages().attachments().get(
                    userId='me', messageId=attachment.message_id, id=attachment.attachment_id).execute()
                encoded = response['data']
            return base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        except Exception as exc:
            raise GmailError(f'Gmail attachment download failed: {exc}') from exc
