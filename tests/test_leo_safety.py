"""Adversarial cases found during independent review of LEO validation."""
import json
from types import SimpleNamespace

import pytest

from extractor import extract_with_ai
from leo_validation import resolve_leo_date
from models import LeoDateEvidence


def observation(label, raw_date, quote):
    return LeoDateEvidence(doc_type="FINAL_LEO", page_number=1, label=label,
                           raw_date=raw_date, quote=quote)


@pytest.mark.parametrize(
    ("page", "label", "raw_date", "quote"),
    [
        ("6.LEO Date 08-JUN-26", "6.LEO Date", "08-JUN-26", "08-JUN-26"),
        ("6.LEO Date 08-JUN-26\n09-JUN-26", "6.LEO Date", "09-JUN-26",
         "6.LEO Date 08-JUN-26\n09-JUN-26"),
        ("16.LEO Date 08-JUN-26", "6.LEO Date", "08-JUN-26", "16.LEO Date 08-JUN-26"),
        ("19.LEO 08-JUN-26", "9.LEO", "08-JUN-26", "19.LEO 08-JUN-26"),
        ("6.LEO Date 08-JUN-26 BRC Date 09-JUN-26", "6.LEO Date", "09-JUN-26",
         "6.LEO Date 08-JUN-26 BRC Date 09-JUN-26"),
        ("6.LEO Date 08-JUN-26\n6.LEO Date 09-JUN-26", "6.LEO Date", "08-JUN-26",
         "6.LEO Date 08-JUN-26"),
    ],
)
def test_unrelated_ambiguous_or_uncited_value_cannot_be_written(page, label, raw_date, quote):
    result = resolve_leo_date([observation(label, raw_date, quote)], {"finalLeo": {1: page}})
    assert result.leo_date is None
    assert result.warning


def test_populated_point_six_with_explicitly_blank_event_is_usable():
    page = "|5.Submission|06-JUN-26|6.LEO Date.|08-JUN-26|\n|9.LEO||12.Vessel Name||"
    result = resolve_leo_date([
        observation("6.LEO Date", "08-JUN-26", "6.LEO Date.|08-JUN-26"),
        observation("9.LEO", None, "9.LEO||12.Vessel Name"),
    ], {"finalLeo": {1: page}})
    assert result.leo_date == "08-Jun-2026"
    assert result.warning is None


def test_ai_cannot_claim_a_populated_event_is_blank():
    page = "6.LEO Date 08-JUN-26\n9.LEO 09-JUN-26"
    result = resolve_leo_date([
        observation("6.LEO Date", "08-JUN-26", "6.LEO Date 08-JUN-26"),
        observation("9.LEO", None, "9.LEO"),
    ], {"finalLeo": {1: page}})
    assert result.leo_date is None
    assert result.warning


def test_invalid_nested_leo_evidence_retains_independent_fields(monkeypatch):
    payload = {"shipper_name": "EXPORTER", "invoice_numbers": "INV-1",
               "container_numbers": None, "container_sizes": None, "customs_seal_numbers": None,
               "leo_evidence": [{"doc_type": "FIRST_COPY", "page_number": 1,
                                 "label": "9.LEO", "raw_date": "06-JUN-26", "quote": "9.LEO 06-JUN-26"}]}
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    monkeypatch.setattr("extractor.get_openai_client", lambda: client)
    result = extract_with_ai("external API boundary fixture")
    assert result.shipper_name == "EXPORTER"
    assert result.invoice_numbers == "INV-1"
    assert result.leo_date is None
    assert result.leo_evidence == []
    assert result.leo_warning


def test_header_columns_cannot_borrow_submission_date():
    page = "|5.Submission|6.LEO Date|\n|06-JUN-26|08-JUN-26|"
    wrong = resolve_leo_date([observation("6.LEO Date", "06-JUN-26", page)], {"finalLeo": {1: page}})
    correct = resolve_leo_date([observation("6.LEO Date", "08-JUN-26", page)], {"finalLeo": {1: page}})
    assert wrong.leo_date is None
    assert wrong.warning
    assert correct.leo_date == "08-Jun-2026"


def test_merged_event_label_keeps_its_next_value_cell():
    page = ("|5.Submission|29-MAR-26|17:55|6.LEO Date.|01-APR-26|\n"
            "|J.PROCESS 9.LEO 10. SEZ UNIT Details|01-APR-26|17:17|12.Vessel Name||")
    result = resolve_leo_date([
        observation("6.LEO Date", "01-APR-26", page.splitlines()[0]),
        observation("9.LEO", "01-APR-26", page.splitlines()[1]),
    ], {"finalLeo": {1: page}})
    assert result.leo_date == "01-Apr-2026"
    assert result.warning is None


def test_a_populated_header_cell_cannot_be_claimed_blank_to_hide_conflict():
    page = "|6.LEO Date|9.LEO|\n|---|---|\n|08-JUN-26|09-JUN-26|"
    result = resolve_leo_date([
        observation("6.LEO Date", None, page),
        observation("9.LEO", "09-JUN-26", page),
    ], {"finalLeo": {1: page}})
    assert result.leo_date is None
    assert result.warning
