import json
import logging
import re
import threading
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError

from config import OPENROUTER_BASE_URL, OPENROUTER_MODEL, require_openrouter_key
from models import CustomsExtractedFields

logger = logging.getLogger(__name__)

_client_instance: OpenAI | None = None
_client_lock = threading.Lock()


def get_openai_client() -> OpenAI:
    """Reuses a cached OpenAI client to preserve HTTP keep-alive connection pools."""
    global _client_instance
    api_key = require_openrouter_key()
    if _client_instance is None or _client_instance.api_key != api_key:
        with _client_lock:
            if _client_instance is None or _client_instance.api_key != api_key:
                _client_instance = OpenAI(
                    base_url=OPENROUTER_BASE_URL,
                    api_key=api_key,
                    default_headers={
                        "HTTP-Referer": "https://github.com/daily-report-local",
                        "X-Title": "Daily Report Local Sync",
                    },
                )
    return _client_instance


def _clean_json_payload(raw: str) -> str:
    """Robustly extracts valid JSON substring from markdown fences, think blocks, or conversational wrapper."""
    cleaned = str(raw or "").strip()
    if not cleaned:
        return "{}"
    # Strip <think>...</think> reasoning blocks if present
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned).strip()
    if "<think>" in cleaned:
        # Unclosed think tag: discard up to the first markdown fence or opening brace
        cleaned = re.sub(r"^[\s\S]*?<think>[\s\S]*?(?=(?:```|\{))", "", cleaned).strip()
    # Find markdown JSON fences
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned, re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
    else:
        # Find outermost curly braces
        obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
        candidate = obj_match.group(1).strip() if obj_match else re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip() or "{}"
    # Clean trailing commas before closing braces/brackets (invalid in standard JSON)
    candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
    return candidate


UNIFIED_PROMPT = (
    "You are an expert Indian Customs ICEGATE document parser.\n"
    "Extract the required customs fields strictly from the document text provided below.\n"
    "If a scalar field is not present or cannot be determined, set it to null. "
    "Always return leo_evidence as an array, empty only when no eligible LEO evidence exists.\n\n"
    "Field Guidelines:\n"
    "- 'shipper_name': TRUE exporter entity name ONLY (found after discharge-port line like 'XXXX (Port Name)' in SB21/SB22, or after/under '4.CHA' in Gatepass). NEVER the CHA company name (e.g. VISHAL LOGISTICS) or '3.Exporter' block. Exclude addresses, roads, PIN, city.\n"
    "- 'invoice_numbers': Primary invoice number from '2.INV NO.' in SB21/SB22 (e.g. NF/156/2025-26, 132/2026, 3878). If multiple invoices, take the first/primary invoice only. Exclude dates, amounts, currency.\n"
    "- 'leo_evidence': For every available eligible source observation, return an object with doc_type, original page number (one-based, page 1 for LEO), label, raw_date, and exact quote. Use Gatepass '1.Order No / date'; for SB22 report both '6.LEO Date' and '9.LEO' when those labels appear. Include explicitly blank eligible fields with raw_date=null and a quote proving the blank; never silently omit a labelled field. The exact quote must contain the label and the associated date token. For table evidence, quote the full exact original Markdown pipe-delimited row, preserving empty cells and every | column delimiter. For the merged Gatepass row beginning 'A LEO DTLS', include the full source row with the prefix, order ID, officer name, and date cell (even when blank). Do not construct, reorder, or combine quote text from separate pieces. Do not borrow a nearby date. Do not choose the Submission, Assessment, SB, BRC, or other date from the same row. Do not return First Copy (SB21) evidence.\n"
    "- LEO output date is derived only after runtime evidence validation; do not return a separate leo_date. If ONLY First Copy (SB21) is available, LEO has NOT yet been granted and leo_evidence must be empty. NEVER use dates under J.PROCESS DETAILS or Submission/Assessment in SB21.\n"
    "- 'container_numbers': Container number(s) from the 'Allowed for Movement' table in Gatepass (e.g. UNSU0088111). Join multiple with newline.\n"
    "- 'container_sizes': ISO container size(s) (20, 40, or 45) from Gatepass 'Allowed for Movement' table. Join multiple with newline.\n"
    "- 'customs_seal_numbers': Customs seal number(s) from Gatepass 'Allowed for Movement' table. Join multiple with newline. NEVER 'Pkg' or label words."
)


def _build_prompt(doc_type: str = "COMBINED") -> str:
    """Share extraction rules; single-document callers add source restrictions."""
    restrictions = {
        "EGATEPASS": "Customs Gatepass (SB35). invoice_numbers must be null.",
        "FINAL_LEO": "Final LEO Shipping Bill (SB22). Containers, sizes and seals must be null.",
        "FIRST_COPY": "First Copy Shipping Bill (SB21). LEO is not granted yet on First Copy; "
                      "leo_evidence must be empty and containers, sizes and seals must be null.",
    }
    restriction = restrictions.get(doc_type)
    return UNIFIED_PROMPT + (f"\nDocument Type: {restriction}\n" if restriction else "")


def _parse_response(raw: str, sb_no: str) -> CustomsExtractedFields:
    """Reject bad LEO evidence without deleting independently valid data fields."""
    try:
        return CustomsExtractedFields.model_validate_json(raw)
    except ValidationError:
        cleaned = _clean_json_payload(raw)
        try:
            return CustomsExtractedFields.model_validate_json(cleaned)
        except ValidationError as exc:
            if all(error["loc"] and error["loc"][0] == "leo_evidence" for error in exc.errors()):
                payload = json.loads(cleaned)
                payload["leo_evidence"] = []
                result = CustomsExtractedFields.model_validate(payload)
                result.leo_warning = "AI returned malformed LEO evidence"
                return result
            logger.warning("Could not validate AI JSON response for SB %s: %s", sb_no, exc)
            return CustomsExtractedFields()


def get_strict_json_schema() -> dict[str, Any]:
    """Generates a strict AI response schema without runtime-derived LEO fields."""
    schema = CustomsExtractedFields.model_json_schema()

    for field_name in ("leo_date", "leo_warning"):
        schema.get("properties", {}).pop(field_name, None)

    def make_strict(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                properties = node.get("properties", {})
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema


def extract_with_ai(
    text: str,
    doc_type: str = "COMBINED",
    sb_no: str = "",
    max_retries: int = 3,
) -> CustomsExtractedFields:
    """
    Extracts customs fields using pure AI (OpenRouter) with strict structured JSON schema.
    Fails fast if OPENROUTER_API_KEY is not configured.
    Includes exponential backoff retry for transient network/rate limit errors.
    """
    prompt = _build_prompt(doc_type)
    client = get_openai_client()

    user_content = f"{prompt}\n\nDocument Text Context:\n{text}"
    messages = [{"role": "user", "content": user_content}]
    schema = get_strict_json_schema()

    response = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "customs_fields", "strict": True, "schema": schema},
                },
                temperature=0.1,
            )
            break
        except (APIConnectionError, RateLimitError, InternalServerError) as e:
            if attempt == max_retries - 1:
                logger.error(f"OpenRouter extraction failed after {max_retries} attempts for SB {sb_no}: {e}")
                raise RuntimeError(f"OpenRouter AI extraction failed for SB {sb_no} ({doc_type}): {e}") from e
            sleep_s = 2 ** attempt
            logger.warning(f"OpenRouter transient error ({e}), retrying in {sleep_s}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(sleep_s)
        except APIStatusError as e:
            if (e.status_code in (408, 429) or e.status_code >= 500) and attempt < max_retries - 1:
                sleep_s = 2 ** attempt
                logger.warning(f"OpenRouter HTTP {e.status_code} error, retrying in {sleep_s}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(sleep_s)
            else:
                logger.error(f"OpenRouter status error for SB {sb_no}: {e}")
                raise RuntimeError(f"OpenRouter AI extraction failed for SB {sb_no} ({doc_type}): {e}") from e
        except Exception as e:
            logger.error(f"OpenRouter unexpected error for SB {sb_no} ({doc_type}): {e}")
            raise RuntimeError(f"OpenRouter AI extraction failed for SB {sb_no} ({doc_type}): {e}") from e

    content = (
        response.choices[0].message.content
        if (response and response.choices and response.choices[0].message)
        else None
    )
    raw = str(content) if content is not None else "{}"
    parsed = _parse_response(raw, sb_no)

    # This is runtime-derived only after source and page evidence is validated.
    parsed.leo_date = None

    # Enforce document-type invariants
    if doc_type == "EGATEPASS":
        parsed.invoice_numbers = None
    elif doc_type == "FIRST_COPY":
        parsed.leo_date = None
        parsed.container_numbers = None
        parsed.container_sizes = None
        parsed.customs_seal_numbers = None
    elif doc_type == "FINAL_LEO":
        parsed.container_numbers = None
        parsed.container_sizes = None
        parsed.customs_seal_numbers = None

    return parsed
