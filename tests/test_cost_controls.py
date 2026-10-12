"""Cost controls must preserve source validation and explicit refresh behaviour."""

from types import SimpleNamespace

import pytest

from models import CustomsExtractedFields, LeoDateEvidence
from pipeline_cache import PipelineCache
from sync import PipelineMetrics, _fetch_and_extract_worker, process_documents


def _evidence(value="08-JUN-26"):
    return LeoDateEvidence(
        doc_type="EGATEPASS", page_number=1, label="1.Order No / date",
        raw_date=value, quote="1.Order No / date 1/101" + (f" {value}" if value else ""),
    )


def _setup(monkeypatch, value="08-JUN-26", warning=None):
    calls = []
    source = "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n" + _evidence(value).quote
    monkeypatch.setattr("sync.trim_pdf", lambda *_a, **_kw: (source, [0]))

    def extract(*_a, **_kw):
        calls.append(True)
        return CustomsExtractedFields(
            invoice_numbers="INV-1", leo_evidence=[_evidence(value)], leo_warning=warning,
        )

    monkeypatch.setattr("sync.extract_with_ai", extract)
    return calls


def test_full_extraction_supplies_later_leo_only_without_another_ai_call(monkeypatch, tmp_path):
    calls = _setup(monkeypatch)
    cache = PipelineCache(tmp_path)
    docs = {"eGatepass": ("gatepass.pdf", b"same")}
    process_documents(docs, sb_no="1234567", cache=cache)
    result = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
    assert result.leo_date == "08-Jun-2026"
    assert len(calls) == 1
    assert result.invoice_numbers is None


def test_rejected_unchanged_evidence_is_revalidated_without_paying_again(monkeypatch, tmp_path):
    calls = _setup(monkeypatch)
    monkeypatch.setattr("sync.trim_pdf", lambda *_a, **_kw: (
        "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n1.Order No / date 1/101 09-JUN-26", [0],
    ))
    docs = {"eGatepass": ("gatepass.pdf", b"same")}
    cache = PipelineCache(tmp_path)
    for _ in range(2):
        result = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
        assert result.leo_date is None
        assert "unverified" in result.leo_warning
    assert len(calls) == 1
    process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache, refresh=True)
    assert len(calls) == 2


def test_changed_pdf_retries_unresolved_evidence(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, value=None)
    cache = PipelineCache(tmp_path)
    for raw in (b"same", b"same", b"new"):
        result = process_documents(
            {"eGatepass": ("gatepass.pdf", raw)}, sb_no="1234567", fields={"leo_date"}, cache=cache,
        )
        assert result.leo_date is None
    assert len(calls) == 2


@pytest.mark.parametrize("blank", [True, False])
def test_full_unresolved_result_supplies_later_leo_only_without_another_call(monkeypatch, tmp_path, blank):
    calls = _setup(monkeypatch, value=None if blank else "08-JUN-26")
    if not blank:
        monkeypatch.setattr("sync.trim_pdf", lambda *_a, **_kw: (
            "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n1.Order No / date 1/101 09-JUN-26", [0],
        ))
    cache = PipelineCache(tmp_path)
    docs = {"eGatepass": ("gatepass.pdf", b"same")}
    first = process_documents(docs, sb_no="1234567", cache=cache)
    result = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
    assert result.leo_date is None
    assert result.leo_warning == first.leo_warning
    assert len(calls) == 1


def test_malformed_ai_is_not_saved_as_an_unresolved_source(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, warning="AI omitted requested fields")
    for _ in range(2):
        process_documents(
            {"eGatepass": ("gatepass.pdf", b"same")}, sb_no="1234567", fields={"leo_date"},
            cache=PipelineCache(tmp_path),
        )
    assert len(calls) == 2


def test_worker_recovers_historical_date_only_after_validated_blanks(monkeypatch):
    latest = {"eGatepass": ("latest.pdf", b"blank")}
    older = {"eGatepass": ("older.pdf", b"dated")}
    def source(raw):
        return "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n" + _evidence(
            None if raw == b"blank" else "08-JUN-26",
        ).quote
    monkeypatch.setattr("sync.trim_pdf", lambda raw, *_a, **_kw: (source(raw), [0]))
    monkeypatch.setattr("sync.extract_with_ai", lambda text, **_kw: CustomsExtractedFields(
        leo_evidence=[_evidence(None if "latest.pdf" in text else "08-JUN-26")],
    ))
    gmail = SimpleNamespace(fetch_documents=lambda *_a, **_kw: latest,
                            iter_leo_versions=lambda *_a, **_kw: iter([older]))
    *_prefix, docs, extracted, error = _fetch_and_extract_worker(2, "1234567", ["leo_date"], gmail)
    assert error is None
    assert extracted.leo_date == "08-Jun-2026"
    assert docs == older
    assert "older.pdf" in extracted.leo_source_note


@pytest.mark.parametrize("value,warning", [("08-JUN-26", None), (None, "AI omitted requested fields")])
def test_worker_does_not_consult_history_for_dated_or_malformed_sources(monkeypatch, value, warning):
    _setup(monkeypatch, value, warning)

    def forbidden(*_a, **_kw):
        raise AssertionError("history must not be consulted")

    gmail = SimpleNamespace(
        fetch_documents=lambda *_a, **_kw: {"eGatepass": ("latest.pdf", b"same")},
        iter_leo_versions=forbidden,
    )
    *_, error = _fetch_and_extract_worker(2, "1234567", ["leo_date"], gmail)
    assert error is None


def test_metrics_report_real_usage_without_inventing_missing_cost():
    metrics = PipelineMetrics()
    metrics.add_usage({"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001})
    metrics.add_usage({"prompt_tokens": 50, "completion_tokens": 10})
    summary = metrics.summary(1)
    assert "150 input" in summary
    assert "30 output" in summary
    assert "$0.001000" in summary
    assert "1/2" in summary


def test_existing_full_cache_can_seed_leo_reuse(monkeypatch, tmp_path):
    calls = _setup(monkeypatch)
    cache = PipelineCache(tmp_path)
    docs = {"eGatepass": ("gatepass.pdf", b"same")}
    process_documents(docs, sb_no="1234567", cache=cache)
    # Simulate an older cache, which has the full result but no LEO-only alias.
    for path in tmp_path.glob("*.json"):
        import hashlib
        if path.name.startswith(hashlib.sha256(b"leo_observations").hexdigest()):
            path.unlink()
    process_documents(docs, sb_no="1234567", cache=cache)
    result = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
    assert result.leo_date == "08-Jun-2026"
    assert len(calls) == 1


def test_cross_request_leo_reuse_is_revalidated_and_refreshable(monkeypatch, tmp_path):
    calls = _setup(monkeypatch)
    docs = {"eGatepass": ("gatepass.pdf", b"same")}
    cache = PipelineCache(tmp_path)
    process_documents(docs, sb_no="1234567", cache=cache)
    # Force a newly converted current context; stale evidence must be rejected.
    monkeypatch.setattr("sync._cached_conversion", lambda *_a, **_kw: (
        "--- SOURCE PAGE 1 ---\nShipping Bill No: 1234567\n1.Order No / date 1/101 09-JUN-26", [0],
    ))
    result = process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache)
    assert result.leo_date is None
    assert "unverified" in result.leo_warning
    assert len(calls) == 1
    process_documents(docs, sb_no="1234567", fields={"leo_date"}, cache=cache, refresh=True)
    assert len(calls) == 2


def test_history_rejection_blocks_search_past_that_version(monkeypatch):
    _setup(monkeypatch, value=None)

    def versions(*_a):
        yield {"eGatepass": ("bad.pdf", b"bad")}
        raise AssertionError("must stop after unverified evidence")

    def extract(text, **_kw):
        return CustomsExtractedFields(leo_evidence=[_evidence("08-JUN-26" if "bad.pdf" in text else None)])

    monkeypatch.setattr("sync.extract_with_ai", extract)
    gmail = SimpleNamespace(
        fetch_documents=lambda *_a, **_kw: {"eGatepass": ("latest.pdf", b"same")},
        iter_leo_versions=versions,
    )
    *_, result, error = _fetch_and_extract_worker(2, "1234567", ["leo_date"], gmail)
    assert error is None
    assert result.leo_date is None
    assert "unverified" in result.leo_warning
