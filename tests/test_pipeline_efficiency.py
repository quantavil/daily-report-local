"""Focused coverage for selective conversion, safe caches, and SB21 fallback."""

from models import CustomsExtractedFields, LeoDateEvidence
from pdf_processor import PdfConversionError
from sync import _fetch_and_extract_worker, process_documents


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get_json(self, namespace, key):
        return self.values.get((namespace, key))

    def put_json(self, namespace, key, value):
        self.values[(namespace, key)] = value

    def get_bytes(self, namespace, key):
        return self.values.get((namespace, key))

    def put_bytes(self, namespace, key, value):
        self.values[(namespace, key)] = value


def test_leo_profile_skips_sb21_and_scopes_ai_to_leo(monkeypatch):
    conversions = []
    ai_calls = []

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append((raw_bytes, doc_type, leo_only))
        return f"--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n{doc_type}", [0]

    def extract(text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append((text, doc_type, sb_no, fields))
        return CustomsExtractedFields()

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)

    process_documents(
        {
            "eGatepass": ("gatepass.pdf", b"gatepass"),
            "finalLeo": ("sb22.pdf", b"sb22"),
            "firstCopy": ("sb21.pdf", b"sb21"),
        },
        sb_no="1234567",
        fields={"leo_date"},
    )

    assert [doc_type for _raw, doc_type, _leo_only in conversions] == ["EGATEPASS", "FINAL_LEO"]
    assert all(leo_only for _raw, _doc_type, leo_only in conversions)
    assert ai_calls[0][3] == {"leo_date"}
    assert "FIRST_COPY" not in ai_calls[0][0]


def test_process_cache_reuses_exact_source_and_invalidates_changed_bytes(monkeypatch):
    conversions = []
    ai_calls = []
    cache = MemoryCache()

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append((raw_bytes, doc_type, leo_only))
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n2.INV NO. INV-1", [0]

    def extract(text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append((text, fields))
        return CustomsExtractedFields(invoice_numbers="INV-1", shipper_name="Exporter")

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    doc = {"finalLeo": ("sb22.pdf", b"same-bytes")}

    cold = process_documents(doc, sb_no="1234567", fields={"inv_no", "shipper_name"}, cache=cache)
    warm = process_documents(doc, sb_no="1234567", fields={"inv_no", "shipper_name"}, cache=cache)
    revised = process_documents(
        {"finalLeo": ("sb22.pdf", b"revised-bytes")},
        sb_no="1234567",
        fields={"inv_no", "shipper_name"},
        cache=cache,
    )

    assert cold.invoice_numbers == warm.invoice_numbers == revised.invoice_numbers == "INV-1"
    assert len(conversions) == 2
    assert len(ai_calls) == 2


def test_refresh_bypasses_ai_cache_but_keeps_conversion_cache(monkeypatch):
    conversions = []
    ai_calls = []
    cache = MemoryCache()

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append((raw_bytes, doc_type, leo_only))
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n2.INV NO. INV-1", [0]

    def extract(_text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append(set(fields or ()))
        return CustomsExtractedFields(invoice_numbers="INV-1")

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    docs = {"finalLeo": ("sb22.pdf", b"sb22")}

    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)
    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)
    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache, refresh=True)

    assert len(conversions) == 1
    assert len(ai_calls) == 2


def test_tampered_ai_cache_record_is_a_miss(monkeypatch):
    cache = MemoryCache()
    ai_calls = []
    monkeypatch.setattr(
        "sync.trim_pdf",
        lambda *_args, **_kwargs: ("--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567", [0]),
    )

    def extract(*_args, **_kwargs):
        ai_calls.append(True)
        return CustomsExtractedFields(invoice_numbers="INV-REAL")

    monkeypatch.setattr("sync.extract_with_ai", extract)
    docs = {"finalLeo": ("sb22.pdf", b"sb22")}
    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)
    ai_entry = next(value for (namespace, _key), value in cache.values.items() if namespace == "ai")
    ai_entry["fields"]["invoice_numbers"] = "INV-TAMPERED"

    result = process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)

    assert result.invoice_numbers == "INV-REAL"
    assert len(ai_calls) == 2


def test_tampered_conversion_cache_record_is_a_miss(monkeypatch):
    cache = MemoryCache()
    conversions = []
    monkeypatch.setattr("sync.extract_with_ai", lambda *_args, **_kwargs: CustomsExtractedFields())

    def trim(*_args, **_kwargs):
        conversions.append(True)
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567", [0]

    monkeypatch.setattr("sync.trim_pdf", trim)
    docs = {"finalLeo": ("sb22.pdf", b"sb22")}
    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)
    conversion_entry = next(
        value for (namespace, _key), value in cache.values.items() if namespace == "conversion"
    )
    conversion_entry["text"] += "\nTampered cache text"

    process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)

    assert len(conversions) == 2


def test_cached_leo_evidence_is_resolved_against_current_context(monkeypatch):
    cache = MemoryCache()
    resolve_calls = []
    gatepass_text = (
        "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n"
        "1.Order No / date 1/101 08-JUN-26"
    )

    monkeypatch.setattr("sync.trim_pdf", lambda *_args, **_kwargs: (gatepass_text, [0]))
    monkeypatch.setattr(
        "sync.extract_with_ai",
        lambda *_args, **_kwargs: CustomsExtractedFields(leo_evidence=[LeoDateEvidence(
            doc_type="EGATEPASS",
            page_number=1,
            label="1.Order No / date",
            raw_date="08-JUN-26",
            quote="1.Order No / date 1/101 08-JUN-26",
        )]),
    )
    original_resolve = __import__("sync").resolve_leo_date

    def counted_resolve(evidence, contexts):
        resolve_calls.append((evidence, contexts))
        return original_resolve(evidence, contexts)

    monkeypatch.setattr("sync.resolve_leo_date", counted_resolve)
    docs = {"eGatepass": ("gatepass.pdf", b"gatepass")}

    first = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
    second = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)

    assert first.leo_date == second.leo_date == "08-Jun-2026"
    assert len(resolve_calls) == 2


def test_warning_bearing_non_leo_ai_result_is_not_reused(monkeypatch):
    cache = MemoryCache()
    ai_calls = []

    monkeypatch.setattr(
        "sync.trim_pdf",
        lambda *_args, **_kwargs: ("--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567", [0]),
    )

    def extract(*_args, **_kwargs):
        ai_calls.append(True)
        if len(ai_calls) == 1:
            return CustomsExtractedFields(leo_warning="malformed AI response")
        return CustomsExtractedFields(invoice_numbers="INV-RECOVERED")

    monkeypatch.setattr("sync.extract_with_ai", extract)
    docs = {"finalLeo": ("sb22.pdf", b"sb22")}

    failed = process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)
    recovered = process_documents(docs, sb_no="1234567", fields={"inv_no"}, cache=cache)

    assert failed.invoice_numbers is None
    assert recovered.invoice_numbers == "INV-RECOVERED"
    assert len(ai_calls) == 2


def test_conversion_failure_does_not_cache_partial_non_leo_ai_output(monkeypatch):
    cache = MemoryCache()
    ai_calls = []
    conversions = []

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append(doc_type)
        if raw_bytes == b"broken-sb22":
            raise PdfConversionError("conversion failed")
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n4.CHA EXPORTER", [0]

    def extract(*_args, **_kwargs):
        ai_calls.append(True)
        return CustomsExtractedFields(shipper_name="GATEPASS EXPORTER")

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    docs = {
        "eGatepass": ("gatepass.pdf", b"gatepass"),
        "finalLeo": ("sb22.pdf", b"broken-sb22"),
    }

    process_documents(docs, sb_no="1234567", fields={"shipper_name"}, cache=cache)
    process_documents(docs, sb_no="1234567", fields={"shipper_name"}, cache=cache)

    assert len(ai_calls) == 2
    assert conversions.count("FINAL_LEO") == 2


def test_unreadable_sb22_uses_scoped_sb21_fallback_without_leo_overwrite(monkeypatch):
    class Gmail:
        def __init__(self):
            self.calls = []

        def fetch_documents(self, sb_no, *, fields=None, include_first_copy=False):
            self.calls.append((sb_no, fields, include_first_copy))
            if include_first_copy:
                return {"firstCopy": ("sb21.pdf", b"sb21")}
            return {
                "eGatepass": ("gatepass.pdf", b"gatepass"),
                "finalLeo": ("sb22.pdf", b"unreadable-sb22"),
            }

    conversions = []
    ai_calls = []

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append((raw_bytes, doc_type, leo_only))
        if raw_bytes == b"unreadable-sb22":
            raise PdfConversionError("SB22 page needs OCR")
        if raw_bytes == b"gatepass":
            return (
                "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n"
                "1.Order No / date 1/101 08-JUN-26\nAllowed for Movement",
                [0],
            )
        return ("--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n2.INV NO. SB21-INV", [0])

    def extract(text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append((text, set(fields or ())))
        if "First Copy Shipping Bill (SB21)" in text:
            return CustomsExtractedFields(invoice_numbers="SB21-INV", shipper_name="SB21 EXPORTER")
        return CustomsExtractedFields(
            container_numbers="TCKU1234567",
            container_sizes="40",
            customs_seal_numbers="SEAL-1",
            leo_evidence=[LeoDateEvidence(
                doc_type="EGATEPASS",
                page_number=1,
                label="1.Order No / date",
                raw_date="08-JUN-26",
                quote="1.Order No / date 1/101 08-JUN-26",
            )],
        )

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    gmail = Gmail()

    row, sb_no, _fields, docs, extracted, error = _fetch_and_extract_worker(
        2,
        "1234567",
        ["inv_no", "leo_date", "shipper_name", "container_no", "container_size", "customs_seal"],
        gmail,
    )

    assert (row, sb_no, error) == (2, "1234567", None)
    assert extracted.invoice_numbers == "SB21-INV"
    assert extracted.shipper_name == "SB21 EXPORTER"
    assert extracted.leo_date is None
    assert extracted.leo_warning
    assert extracted.container_numbers == "TCKU1234567"
    assert extracted.container_sizes == "40"
    assert extracted.customs_seal_numbers == "SEAL-1"
    assert gmail.calls == [
        ("1234567", {"inv_no", "leo_date", "shipper_name", "container_no", "container_size", "customs_seal"}, False),
        ("1234567", {"inv_no", "shipper_name"}, True),
    ]
    assert [fields for _text, fields in ai_calls] == [
        {"inv_no", "leo_date", "shipper_name", "container_no", "container_size", "customs_seal"},
        {"inv_no", "shipper_name"},
    ]
    assert [doc_type for _raw, doc_type, _leo_only in conversions] == ["EGATEPASS", "FINAL_LEO", "FIRST_COPY"]
    assert "firstCopy" in docs


def test_sb21_only_bill_fetches_scoped_first_copy_after_empty_initial_search(monkeypatch):
    class Gmail:
        def __init__(self):
            self.calls = []

        def fetch_documents(self, sb_no, *, fields=None, include_first_copy=False):
            self.calls.append((sb_no, fields, include_first_copy))
            return {"firstCopy": ("sb21.pdf", b"sb21")} if include_first_copy else {}

    conversions = []
    ai_calls = []

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append(doc_type)
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n2.INV NO. SB21-INV", [0]

    def extract(_text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append(set(fields or ()))
        return CustomsExtractedFields(invoice_numbers="SB21-INV", shipper_name="SB21 EXPORTER")

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    gmail = Gmail()

    _row, _sb, _fields, docs, extracted, error = _fetch_and_extract_worker(
        2, "1234567", ["inv_no", "leo_date", "shipper_name"], gmail
    )

    assert error is None
    assert docs.keys() == {"firstCopy"}
    assert extracted.invoice_numbers == "SB21-INV"
    assert extracted.shipper_name == "SB21 EXPORTER"
    assert extracted.leo_date is None
    assert gmail.calls == [
        ("1234567", {"inv_no", "leo_date", "shipper_name"}, False),
        ("1234567", {"inv_no", "shipper_name"}, True),
    ]
    assert conversions == ["FIRST_COPY"]
    assert ai_calls == [{"inv_no", "shipper_name"}]


def test_gatepass_without_sb22_falls_back_only_for_missing_invoice(monkeypatch):
    class Gmail:
        def __init__(self):
            self.calls = []

        def fetch_documents(self, sb_no, *, fields=None, include_first_copy=False):
            self.calls.append((set(fields or ()), include_first_copy))
            if include_first_copy:
                return {"firstCopy": ("sb21.pdf", b"sb21")}
            return {"eGatepass": ("gatepass.pdf", b"gatepass")}

    conversions = []
    ai_calls = []

    def trim(raw_bytes, doc_type, *, leo_only=False):
        conversions.append(doc_type)
        text = "Shipping Bill No: 1234567"
        if raw_bytes == b"gatepass":
            text += "\n4.CHA EXPORTER"
        else:
            text += "\n2.INV NO. SB21-INV"
        return f"--- SOURCE PAGE 1 ---\n{text}", [0]

    def extract(text, doc_type="COMBINED", sb_no="", fields=None):
        ai_calls.append((set(fields or ()), "First Copy Shipping Bill (SB21)" in text))
        if "First Copy Shipping Bill (SB21)" in text:
            return CustomsExtractedFields(invoice_numbers="SB21-INV")
        return CustomsExtractedFields(shipper_name="GATEPASS EXPORTER")

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)
    gmail = Gmail()

    _row, _sb, _fields, docs, extracted, error = _fetch_and_extract_worker(
        2, "1234567", ["inv_no", "shipper_name"], gmail
    )

    assert error is None
    assert extracted.invoice_numbers == "SB21-INV"
    assert extracted.shipper_name == "GATEPASS EXPORTER"
    assert docs.keys() == {"eGatepass", "firstCopy"}
    assert gmail.calls == [({"inv_no", "shipper_name"}, False), ({"inv_no"}, True)]
    assert conversions == ["EGATEPASS", "FIRST_COPY"]
    assert ai_calls == [({"inv_no", "shipper_name"}, False), ({"inv_no"}, True)]


def test_malformed_sb21_fallback_keeps_warning_and_validated_leo(monkeypatch):
    class Gmail:
        def fetch_documents(self, sb_no, *, fields=None, include_first_copy=False):
            if include_first_copy:
                return {"firstCopy": ("sb21.pdf", b"sb21")}
            return {
                "eGatepass": ("gatepass.pdf", b"gatepass"),
                "finalLeo": ("sb22.pdf", b"sb22"),
            }

    def trim(raw_bytes, doc_type, *, leo_only=False):
        if raw_bytes == b"gatepass":
            return (
                "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n"
                "1.Order No / date 1/101 08-JUN-26",
                [0],
            )
        return ("--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567", [0])

    def extract(text, doc_type="COMBINED", sb_no="", fields=None):
        if "First Copy Shipping Bill (SB21)" in text:
            return CustomsExtractedFields(leo_warning="AI returned malformed response")
        return CustomsExtractedFields(leo_evidence=[LeoDateEvidence(
            doc_type="EGATEPASS",
            page_number=1,
            label="1.Order No / date",
            raw_date="08-JUN-26",
            quote="1.Order No / date 1/101 08-JUN-26",
        )])

    monkeypatch.setattr("sync.trim_pdf", trim)
    monkeypatch.setattr("sync.extract_with_ai", extract)

    _row, _sb, _fields, _docs, result, error = _fetch_and_extract_worker(
        2, "1234567", ["inv_no", "leo_date"], Gmail()
    )

    assert error is None
    assert result.leo_date == "08-Jun-2026"
    assert result.leo_warning == "AI returned malformed response"
