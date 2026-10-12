import base64
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDENTIALS_FILE, GMAIL_MAX_THREADS, GMAIL_TOKEN_FILE
from pdf_processor import classify_document

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SUBJECT_PHRASES = [
    "Electronic eGatepass copy of SB for Shipping Bill",
    "Electronic Final LEO copy of SB for Shipping Bill",
    "Electronic First copy of SB for Shipping Bill",
]


class GmailClient:
    _auth_lock = threading.Lock()

    def __init__(
        self,
        credentials_file: Path = GMAIL_CREDENTIALS_FILE,
        token_file: Path = GMAIL_TOKEN_FILE,
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._local = threading.local()
        self._creds = None

    @property
    def service(self):
        if not hasattr(self._local, "service") or self._local.service is None:
            if self._creds is None or not self._creds.valid:
                if not self.authenticate():
                    return None
            else:
                self._local.service = build("gmail", "v1", credentials=self._creds, cache_discovery=False)
        return self._local.service

    @service.setter
    def service(self, val):
        self._local.service = val

    def authenticate(self) -> bool:
        """
        Authenticates with Gmail via OAuth2 Desktop flow.
        Uses token.json if present; otherwise launches one-time browser consent.
        """
        with self._auth_lock:
            if self._creds and self._creds.valid:
                if not hasattr(self._local, "service") or self._local.service is None:
                    self._local.service = build("gmail", "v1", credentials=self._creds, cache_discovery=False)
                return True

            creds = None
            if self.token_file.exists():
                try:
                    creds = Credentials.from_authorized_user_file(str(self.token_file), GMAIL_SCOPES)
                except Exception as e:
                    print(f"⚠️ Error loading token.json: {e}")

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception:
                        creds = None

                if not creds:
                    if not self.credentials_file.exists():
                        print(f"ℹ️ Gmail credentials file '{self.credentials_file.name}' not found.")  # noqa: RUF001
                        print("  To enable Gmail downloads, download OAuth Desktop client credentials from Google Cloud Console.")
                        return False

                    flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), GMAIL_SCOPES)
                    creds = flow.run_local_server(port=0)

                with open(self.token_file, "w") as token:
                    token.write(creds.to_json())

            self._creds = creds
            self._local.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return True

    def fetch_documents(self, sb_no: str) -> dict[str, tuple[str, bytes]]:
        """
        Searches Gmail for ICEGATE PDF attachments matching a given Shipping Bill number.
        Returns: { 'eGatepass': (filename, bytes), 'finalLeo': (filename, bytes), 'firstCopy': (filename, bytes) }
        """
        service = self.service
        if not service:
            if not self.authenticate():
                return {}
            service = self.service
        if not service:
            return {}

        phrases_or = " OR ".join(f'"{p}"' for p in SUBJECT_PHRASES)
        query = f'"{sb_no}" ({phrases_or})'

        results = service.users().messages().list(userId="me", q=query, maxResults=GMAIL_MAX_THREADS).execute()
        messages = results.get("messages", [])
        if not messages:
            return {}

        documents: dict[str, tuple[str, bytes]] = {}

        for msg_summary in messages:
            msg = service.users().messages().get(userId="me", id=msg_summary["id"]).execute()
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            subject = headers.get("subject", "")

            # Walk all MIME parts recursively — ICEGATE mails are usually
            # multipart/mixed wrapping multipart/alternative, so PDFs sit
            # 2-3 levels deep, not at payload.parts top level.
            def iter_parts(part):
                yield part
                for sub in part.get("parts", []) or []:
                    yield from iter_parts(sub)

            for part in iter_parts(payload):
                filename = part.get("filename", "")
                mime_type = part.get("mimeType", "")
                if not filename or not (filename.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
                    continue

                body = part.get("body", {})
                attachment_id = body.get("attachmentId")
                raw_data = body.get("data", "")
                if not attachment_id and not raw_data:
                    continue

                slot = classify_document(filename, subject)
                if slot != "UNKNOWN" and slot not in documents:
                    if not raw_data and attachment_id:
                        att_data = service.users().messages().attachments().get(
                            userId="me", messageId=msg_summary["id"], id=attachment_id
                        ).execute()
                        raw_data = att_data.get("data", "")
                    if raw_data:
                        padded_data = raw_data + "=" * (-len(raw_data) % 4)
                        raw_bytes = base64.urlsafe_b64decode(padded_data)
                    else:
                        raw_bytes = b""
                    documents[slot] = (filename, raw_bytes)

                if len(documents) == 3:
                    return documents

        return documents
