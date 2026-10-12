from unittest.mock import MagicMock, patch

import pytest

from config import require_openrouter_key
from extractor import UNIFIED_PROMPT, _build_prompt, extract_with_ai, get_strict_json_schema


def test_require_openrouter_key_raises_when_missing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is missing or empty"):
        require_openrouter_key()


def test_require_openrouter_key_returns_when_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-key")
    assert require_openrouter_key() == "sk-or-v1-test-key"


def test_strict_json_schema_structure():
    schema = get_strict_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "properties" in schema
    assert "required" in schema
    # Every property MUST be present in required for strict JSON schema mode
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert "shipper_name" in schema["required"]
    assert "invoice_numbers" in schema["required"]
    assert "leo_evidence" in schema["required"]
    assert "leo_date" not in schema["properties"]
    assert "container_numbers" in schema["required"]


def test_strict_json_schema_requires_typed_evidence_and_excludes_runtime_leo_fields():
    schema = get_strict_json_schema()
    props = schema["properties"]

    assert "leo_evidence" in schema["required"]
    assert "leo_evidence" in props
    assert "leo_date" not in props
    assert "leo_warning" not in props
    assert props["leo_evidence"]["type"] == "array"

    evidence_schema = props["leo_evidence"]["items"]
    if "$ref" in evidence_schema:
        evidence_name = evidence_schema["$ref"].rsplit("/", 1)[-1]
        evidence_schema = schema["$defs"][evidence_name]
    assert evidence_schema["type"] == "object"
    assert evidence_schema["additionalProperties"] is False
    assert set(evidence_schema["required"]) == {
        "doc_type", "page_number", "label", "raw_date", "quote"
    }


def test_evidence_array_is_not_cleaned_as_a_scalar_placeholder():
    from models import CustomsExtractedFields, LeoDateEvidence

    evidence = LeoDateEvidence(
        doc_type="FINAL_LEO",
        page_number=1,
        label="6.LEO Date",
        raw_date="08-JUN-26",
        quote="6.LEO Date 08-JUN-26",
    )
    fields = CustomsExtractedFields(leo_evidence=[evidence])

    assert fields.leo_evidence == [evidence]


def test_unified_prompt_requests_source_page_and_exact_leo_evidence():
    prompt = _build_prompt("COMBINED")

    assert "leo_evidence" in prompt
    assert "exact quote" in prompt.lower()
    assert "original page number" in prompt.lower()
    assert "6.LEO Date" in prompt
    assert "9.LEO" in prompt


def test_build_prompt_doc_types():
    p_gate = _build_prompt("EGATEPASS")
    assert "Customs Gatepass (SB35)" in p_gate
    assert "4.CHA" in p_gate
    assert "Allowed for Movement" in p_gate

    p_leo = _build_prompt("FINAL_LEO")
    assert "Final LEO Shipping Bill (SB22)" in p_leo
    assert "6.LEO Date" in p_leo
    assert "2.INV NO." in p_leo

    p_first = _build_prompt("FIRST_COPY")
    assert "First Copy Shipping Bill (SB21)" in p_first
    assert "not granted yet on First Copy" in p_first

    p_combined = _build_prompt("COMBINED")
    assert "If ONLY First Copy (SB21) is available, LEO has NOT yet been granted" in p_combined
    assert "NEVER use dates under J.PROCESS DETAILS or Submission/Assessment in SB21" in UNIFIED_PROMPT


def test_extract_with_ai_mock_success(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content='{"shipper_name": "ACME EXPORTS", "invoice_numbers": "INV/001", "leo_date": "10-Sep-2026", "container_numbers": "TCKU1234567", "container_sizes": "40", "customs_seal_numbers": "012345"}'
            )
        )
    ]

    with patch("extractor.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        # EGATEPASS should zero out invoice_numbers
        res_gate = extract_with_ai("dummy text", "EGATEPASS", sb_no="1234567")
        assert res_gate.shipper_name == "ACME EXPORTS"
        assert res_gate.invoice_numbers is None
        assert res_gate.leo_date is None
        assert res_gate.container_numbers == "TCKU1234567"

        # FIRST_COPY should zero out LEO and container info
        res_first = extract_with_ai("dummy text", "FIRST_COPY", sb_no="1234567")
        assert res_first.shipper_name == "ACME EXPORTS"
        assert res_first.invoice_numbers == "INV/001"
        assert res_first.leo_date is None
        assert res_first.container_numbers is None


def test_extract_with_ai_handles_markdown_wrapped_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content='```json\n{"shipper_name": "FENCED EXPORTS", "invoice_numbers": "INV/999", "leo_date": null, "container_numbers": null, "container_sizes": null, "customs_seal_numbers": null}\n```'
            )
        )
    ]

    with patch("extractor.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        res = extract_with_ai("dummy text", "FIRST_COPY", sb_no="1234567")
        assert res.shipper_name == "FENCED EXPORTS"
        assert res.invoice_numbers == "INV/999"


def test_extract_with_ai_retries_transient_error(monkeypatch):
    from openai import RateLimitError

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content='{"shipper_name": "RETRY EXPORTS", "invoice_numbers": "INV/777", "leo_date": null, "container_numbers": null, "container_sizes": null, "customs_seal_numbers": null}'
            )
        )
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        RateLimitError(message="rate limited", response=MagicMock(status_code=429), body=None),
        mock_resp,
    ]

    with patch("extractor.get_openai_client", return_value=mock_client), patch("time.sleep"):
        res = extract_with_ai("dummy text", "FIRST_COPY", sb_no="1234567")
        assert res.shipper_name == "RETRY EXPORTS"
        assert mock_client.chat.completions.create.call_count == 2


def test_clean_json_payload_with_think_and_text():
    from extractor import _clean_json_payload

    raw = """<think>I need to parse ICEGATE fields.</think>
    Here is the JSON result:
    ```json
    {"shipper_name": "ACME EXPORTS", "invoice_numbers": "101"}
    ```
    Best regards."""

    cleaned = _clean_json_payload(raw)
    assert cleaned == '{"shipper_name": "ACME EXPORTS", "invoice_numbers": "101"}'


def test_clean_json_payload_plain_and_empty():
    from extractor import _clean_json_payload

    assert _clean_json_payload("") == "{}"
    assert _clean_json_payload('{"shipper_name": "TEST"}') == '{"shipper_name": "TEST"}'


def test_extract_with_ai_handles_think_tags(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content='<think>Thinking...</think>```json\n{"shipper_name": "THINK EXPORTS", "invoice_numbers": "INV/333", "leo_date": null, "container_numbers": null, "container_sizes": null, "customs_seal_numbers": null}\n```'
            )
        )
    ]

    with patch("extractor.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_client

        res = extract_with_ai("dummy text", "FIRST_COPY", sb_no="1234567")
        assert res.shipper_name == "THINK EXPORTS"
        assert res.invoice_numbers == "INV/333"


def test_get_openai_client_caches_instance(monkeypatch):
    import extractor

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")
    extractor._client_instance = None

    with patch("extractor.OpenAI") as mock_openai_cls:
        mock_instance = MagicMock()
        mock_instance.api_key = "sk-or-v1-mock-key"
        mock_openai_cls.return_value = mock_instance

        client1 = extractor.get_openai_client()
        client2 = extractor.get_openai_client()
        assert client1 is client2
        assert mock_openai_cls.call_count == 1


def test_clean_json_payload_trailing_comma():
    from extractor import _clean_json_payload
    from models import CustomsExtractedFields

    raw = '{"shipper_name": "ACME EXPORTS", "invoice_numbers": "101", "container_numbers": ["TCKU1234567",],}'
    cleaned = _clean_json_payload(raw)
    parsed = CustomsExtractedFields.model_validate_json(cleaned)
    assert parsed.shipper_name == "ACME EXPORTS"
    assert parsed.invoice_numbers == "101"


def test_clean_json_payload_unclosed_think_tag():
    from extractor import _clean_json_payload
    from models import CustomsExtractedFields

    raw = '<think>I have analyzed the document and now output JSON: ```json\n{"shipper_name": "UNCLOSED THINK EXPORTS", "invoice_numbers": "INV/555"}\n```'
    cleaned = _clean_json_payload(raw)
    parsed = CustomsExtractedFields.model_validate_json(cleaned)
    assert parsed.shipper_name == "UNCLOSED THINK EXPORTS"
    assert parsed.invoice_numbers == "INV/555"


def test_extract_with_ai_fallback_unparseable_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content="I'm sorry, I could not extract the data from this document."
            )
        )
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("extractor.get_openai_client", return_value=mock_client):
        res = extract_with_ai("dummy text", "COMBINED", sb_no="1234567")
        assert res.shipper_name is None
        assert res.leo_date is None

