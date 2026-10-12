from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDENTIALS_FILE, GMAIL_MAX_THREADS, GMAIL_TOKEN_FILE
from pdf_processor import classify_document
from pipeline_cache import cache_key

if TYPE_CHECKING:
    from pipeline_cache import PipelineCache

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SUBJECT_PHRASES = [
    "Electronic eGatepass copy of SB for Shipping Bill",
    "Electronic Final LEO copy of SB for Shipping Bill",
    "Electronic First copy of SB for Shipping Bill",
]

WORKBOOK_FIELDS = {
    "inv_no",
    "shipper_name",
    "leo_date",
    "container_no",
    "container_size",
    "customs_seal",
}
FIRST_COPY_FIELDS = {"inv_no", "shipper_name"}
MIME_METADATA_DEPTH = 12


def _mime_part_metadata_fields(depth: int) -> str:
    nested_parts = f",parts({_mime_part_metadata_fields(depth - 1)})" if depth else ""
    return f"filename,mimeType,body(attachmentId,size){nested_parts}"


# Gmail's partial-response mask omits bytes while retaining filenames and IDs.
# Include enough recursive levels for nested multipart/alternative ICEGATE mail.
METADATA_FIELDS = f"id,internalDate,payload(headers,{_mime_part_metadata_fields(MIME_METADATA_DEPTH)})"


class GmailClient:
    _auth_lock = threading.Lock()

    def __init__(
        self,
        credentials_file: Path = GMAIL_CREDENTIALS_FILE,
        token_file: Path = GMAIL_TOKEN_FILE,
        cache: PipelineCache | None = None,
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.cache = cache
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

    def fetch_documents(
        self,
        sb_no: str,
        *,
        fields: set[str] | None = None,
        include_first_copy: bool = False,
    ) -> dict[str, tuple[str, bytes]]:
        """
        Searches Gmail for ICEGATE PDF attachments matching a given Shipping Bill number.
        Returns the newest requested document slots as ``{slot: (filename, bytes)}``.
        Message metadata is always discovered fresh; attachment bytes may be reused from cache.
        """
        requested = WORKBOOK_FIELDS if fields is None else WORKBOOK_FIELDS.intersection(fields)
        wanted_slots = set()
        if requested & {"leo_date", "container_no", "container_size", "customs_seal", "shipper_name"}:
            wanted_slots.add("eGatepass")
        if requested & {"leo_date", "inv_no", "shipper_name"}:
            wanted_slots.add("finalLeo")
        needs_first_copy_fallback = bool(requested & FIRST_COPY_FIELDS)
        if include_first_copy and needs_first_copy_fallback:
            wanted_slots.add("firstCopy")
        if not wanted_slots:
            return {}
        discover_slots = wanted_slots | ({"firstCopy"} if needs_first_copy_fallback else set())

        service = self.service
        if not service:
            if not self.authenticate():
                return {}
            service = self.service
        if not service:
            return {}

        phrases_or = " OR ".join(f'"{p}"' for p in SUBJECT_PHRASES)
        query = f'"{sb_no}" ({phrases_or})'

        messages_api = service.users().messages()
        summaries: dict[str, dict[str, Any]] = {}
        page_token = None
        while True:
            list_args = {"userId": "me", "q": query, "maxResults": GMAIL_MAX_THREADS}
            if page_token:
                list_args["pageToken"] = page_token
            result = messages_api.list(**list_args).execute()
            for summary in result.get("messages", []):
                if summary.get("id"):
                    summaries[summary["id"]] = summary
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        if not summaries:
            return {}

        candidates: dict[str, list[dict[str, Any]]] = {slot: [] for slot in discover_slots}
        for message_id in sorted(summaries):
            metadata = messages_api.get(
                userId="me",
                id=message_id,
                format="full",
                fields=METADATA_FIELDS,
            ).execute()
            payload = metadata.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            subject = headers.get("subject", "")
            try:
                internal_date = int(metadata.get("internalDate", 0))
            except (TypeError, ValueError):
                internal_date = 0

            for part in self._iter_parts(payload):
                filename = part.get("filename", "")
                mime_type = part.get("mimeType", "")
                if not filename or not (filename.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
                    continue
                slot = classify_document(filename, subject)
                if slot not in discover_slots:
                    continue
                body = part.get("body", {})
                attachment_id = body.get("attachmentId")
                raw_data = body.get("data", "")
                if not attachment_id and not raw_data and not body.get("size"):
                    continue
                candidates[slot].append(
                    {
                        "message_id": message_id,
                        "internal_date": internal_date,
                        "subject": subject,
                        "filename": filename,
                        "attachment_id": attachment_id,
                        "raw_data": raw_data,
                    }
                )

        documents: dict[str, tuple[str, bytes]] = {}
        download_slots = set(wanted_slots)
        if needs_first_copy_fallback and not include_first_copy and not candidates["finalLeo"]:
            download_slots.add("firstCopy")
        for slot in sorted(download_slots):
            slot_candidates = candidates[slot]
            if not slot_candidates:
                continue
            newest = min(
                slot_candidates,
                key=lambda candidate: (
                    -candidate["internal_date"],
                    candidate["message_id"],
                    candidate["filename"].casefold(),
                    candidate["attachment_id"] or "",
                ),
            )
            raw_bytes = self._download_candidate(messages_api, newest, slot)
            if raw_bytes is not None:
                documents[slot] = (newest["filename"], raw_bytes)
        return documents

    @staticmethod
    def _iter_parts(part: dict[str, Any]):
        yield part
        for subpart in part.get("parts", []) or []:
            yield from GmailClient._iter_parts(subpart)

    def _download_candidate(
        self,
        messages_api: Any,
        candidate: dict[str, Any],
        slot: str,
    ) -> bytes | None:
        message_id = candidate["message_id"]
        attachment_id = candidate["attachment_id"]
        raw_data = candidate["raw_data"]

        if raw_data:
            return self._decode_data(raw_data)

        if attachment_id:
            cache_id = cache_key(
                message_id,
                attachment_id,
                slot,
                candidate["filename"],
            )
            cached = None
            if self.cache is not None:
                try:
                    cached = self.cache.get_bytes("gmail_attachments", cache_id)
                except Exception:
                    cached = None
            if cached is not None:
                return cached

            response = messages_api.attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
            raw_data = response.get("data", "")
            if not raw_data:
                return None
            raw_bytes = self._decode_data(raw_data)
            if self.cache is not None:
                try:
                    self.cache.put_bytes("gmail_attachments", cache_id, raw_bytes)
                except Exception:
                    pass
            return raw_bytes

        # Small inline attachments have body.data instead of an attachment ID.
        # Retrieve full content only for the already selected message.
        message = messages_api.get(userId="me", id=message_id, format="full").execute()
        for part in self._iter_parts(message.get("payload", {})):
            if part.get("filename") != candidate["filename"]:
                continue
            if classify_document(part.get("filename", ""), candidate["subject"]) != slot:
                continue
            data = part.get("body", {}).get("data", "")
            if data:
                return self._decode_data(data)
        return None

    @staticmethod
    def _decode_data(raw_data: str) -> bytes:
        padded_data = raw_data + "=" * (-len(raw_data) % 4)
        return base64.urlsafe_b64decode(padded_data)
