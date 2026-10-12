import base64
from copy import deepcopy

from gmail_client import GmailClient


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeAttachments:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def get(self, *, userId, messageId, id):
        self.calls.append((messageId, id))
        return FakeRequest(lambda: {"data": self.messages[messageId]["attachment_data"][id]})


class FakeMessages:
    def __init__(self, pages, records):
        self.pages = pages
        self.records = records
        self.list_calls = []
        self.get_calls = []
        self.attachments_api = FakeAttachments(records)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        token = kwargs.get("pageToken")
        return FakeRequest(lambda: self.pages[token])

    def get(self, *, userId, id, **kwargs):
        self.get_calls.append((id, kwargs))

        def response():
            message = deepcopy(self.records[id]["message"])
            if kwargs.get("fields"):
                self._truncate_parts(message.get("payload", {}), kwargs["fields"].count("parts("))
            return message

        return FakeRequest(response)

    @classmethod
    def _truncate_parts(cls, part, remaining_depth):
        if remaining_depth <= 0:
            part.pop("parts", None)
            return
        for child in part.get("parts", []) or []:
            cls._truncate_parts(child, remaining_depth - 1)

    def attachments(self):
        return self.attachments_api


class FakeUsers:
    def __init__(self, messages):
        self.messages_api = messages

    def messages(self):
        return self.messages_api


class FakeService:
    def __init__(self, pages, records):
        self.messages_api = FakeMessages(pages, records)

    def users(self):
        return FakeUsers(self.messages_api)


class FakeCache:
    def __init__(self):
        self.values = {}
        self.get_calls = []
        self.put_calls = []

    def get_bytes(self, namespace, key):
        self.get_calls.append((namespace, key))
        return self.values.get((namespace, key))

    def put_bytes(self, namespace, key, value):
        self.put_calls.append((namespace, key, value))
        self.values[(namespace, key)] = value


def _record(subject, internal_date, filename, attachment_id, data):
    encoded = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    return {
        "message": {
            "id": attachment_id.split("-")[0],
            "internalDate": str(internal_date),
            "payload": {
                "headers": [{"name": "Subject", "value": subject}],
                "parts": [
                    {
                        "filename": filename,
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": attachment_id, "size": len(data)},
                    }
                ],
            },
        },
        "attachment_data": {attachment_id: encoded},
    }


def _client(records, pages=None, cache=None):
    if pages is None:
        pages = {None: {"messages": [{"id": message_id} for message_id in records]}}
    client = GmailClient(cache=cache)
    client.service = FakeService(pages, records)
    return client


def test_fetch_documents_paginates_and_uses_newest_attachment_per_slot():
    records = {
        "old-gp": _record("eGatepass", 100, "old_SB35.pdf", "old-gp-att", b"old"),
        "new-gp": _record("eGatepass", 300, "new_SB35.pdf", "new-gp-att", b"new"),
        "sb22": _record("Final LEO", 200, "bill_SB22.pdf", "sb22-att", b"leo"),
    }
    records["old-gp"]["message"]["id"] = "old-gp"
    records["new-gp"]["message"]["id"] = "new-gp"
    records["sb22"]["message"]["id"] = "sb22"
    pages = {
        None: {"messages": [{"id": "old-gp"}], "nextPageToken": "next"},
        "next": {"messages": [{"id": "new-gp"}, {"id": "sb22"}]},
    }
    client = _client(records, pages)

    documents = client.fetch_documents("1278254")

    assert documents == {
        "eGatepass": ("new_SB35.pdf", b"new"),
        "finalLeo": ("bill_SB22.pdf", b"leo"),
    }
    assert [call.get("pageToken") for call in client.service.messages_api.list_calls] == [None, "next"]


def test_metadata_discovery_walks_deep_nested_mime_parts():
    leaf = {
        "filename": "deep_SB35.pdf",
        "mimeType": "application/pdf",
        "body": {"attachmentId": "deep-att", "size": 4},
    }
    for _ in range(8):
        leaf = {"mimeType": "multipart/mixed", "parts": [leaf]}
    record = {
        "message": {
            "id": "deep-message",
            "internalDate": "100",
            "payload": {
                "headers": [{"name": "Subject", "value": "eGatepass"}],
                "parts": [leaf],
            },
        },
        "attachment_data": {"deep-att": base64.urlsafe_b64encode(b"deep").decode("ascii")},
    }
    client = _client({"deep-message": record})

    documents = client.fetch_documents("1278254", fields={"container_no"})

    assert documents == {"eGatepass": ("deep_SB35.pdf", b"deep")}


def test_fetch_documents_does_not_download_first_copy_when_final_leo_exists():
    records = {
        "sb21": _record("First copy", 100, "bill_SB21.pdf", "sb21-att", b"first"),
        "sb22": _record("Final LEO", 200, "bill_SB22.pdf", "sb22-att", b"final"),
    }
    for message_id, record in records.items():
        record["message"]["id"] = message_id
    client = _client(records)

    documents = client.fetch_documents("1278254")

    assert "firstCopy" not in documents
    assert client.service.messages_api.attachments_api.calls == [("sb22", "sb22-att")]


def test_first_copy_fallback_is_limited_to_invoice_and_shipper_fields():
    records = {
        "sb21": _record("First copy", 100, "bill_SB21.pdf", "sb21-att", b"first"),
    }
    records["sb21"]["message"]["id"] = "sb21"
    client = _client(records)

    documents = client.fetch_documents("1278254", fields={"inv_no"}, include_first_copy=True)

    assert documents == {"firstCopy": ("bill_SB21.pdf", b"first")}
    assert client.service.messages_api.attachments_api.calls == [("sb21", "sb21-att")]

    leo_client = _client(records)
    leo_documents = leo_client.fetch_documents(
        "1278254", fields={"leo_date"}, include_first_copy=True
    )
    assert leo_documents == {}
    assert leo_client.service.messages_api.attachments_api.calls == []


def test_first_copy_is_default_fallback_when_no_final_leo_metadata_exists():
    record = _record("First copy", 100, "bill_SB21.pdf", "sb21-att", b"first")
    record["message"]["id"] = "sb21"
    client = _client({"sb21": record})

    documents = client.fetch_documents("1278254", fields={"inv_no"})

    assert documents == {"firstCopy": ("bill_SB21.pdf", b"first")}
    assert len(client.service.messages_api.list_calls) == 1
    assert client.service.messages_api.attachments_api.calls == [("sb21", "sb21-att")]


def test_fetch_documents_reuses_cached_attachment_bytes_and_invalidates_on_new_attachment_id():
    cache = FakeCache()
    first_record = _record("Final LEO", 100, "bill_SB22.pdf", "sb22-old-att", b"old")
    first_record["message"]["id"] = "sb22-old"
    client = _client({"sb22-old": first_record}, cache=cache)

    assert client.fetch_documents("1278254")["finalLeo"][1] == b"old"
    assert client.fetch_documents("1278254")["finalLeo"][1] == b"old"
    assert client.service.messages_api.attachments_api.calls == [("sb22-old", "sb22-old-att")]

    revised = _record("Final LEO", 200, "bill_SB22.pdf", "sb22-new-att", b"revised")
    revised["message"]["id"] = "sb22-new"
    revised_client = _client({"sb22-new": revised}, cache=cache)
    assert revised_client.fetch_documents("1278254")["finalLeo"][1] == b"revised"
    assert revised_client.service.messages_api.attachments_api.calls == [("sb22-new", "sb22-new-att")]


def test_attachment_cache_does_not_cross_message_ids_with_same_attachment_id():
    cache = FakeCache()
    first = _record("Final LEO", 100, "bill_SB22.pdf", "shared-att", b"first message")
    first["message"]["id"] = "first-message"
    second = _record("Final LEO", 200, "bill_SB22.pdf", "shared-att", b"second message")
    second["message"]["id"] = "second-message"

    first_client = _client({"first-message": first}, cache=cache)
    second_client = _client({"second-message": second}, cache=cache)

    assert first_client.fetch_documents("1278254")["finalLeo"][1] == b"first message"
    assert second_client.fetch_documents("1278254")["finalLeo"][1] == b"second message"
    assert first_client.service.messages_api.attachments_api.calls == [
        ("first-message", "shared-att")
    ]
    assert second_client.service.messages_api.attachments_api.calls == [
        ("second-message", "shared-att")
    ]

