import logging
import re
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


def get_openai_client() -> OpenAI:
    """Reuses a cached OpenAI client to preserve HTTP keep-alive connection pools."""
    global _client_instance
    api_key = require_openrouter_key()
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


UNIFIED_PROMPT = (
    "You are an expert Indian Customs ICEGATE document parser.\n"
    "Extract the required customs fields strictly from the document text provided below.\n"
    "If a field is not present or cannot be determined from the documents, set it to null.\n\n"
    "Field Guidelines:\n"
    "- 'shipper_name': TRUE exporter entity name ONLY (found after discharge-port line like 'XXXX (Port Name)' in SB21/SB22, or after/under '4.CHA' in Gatepass). NEVER the CHA company name (e.g. VISHAL LOGISTICS) or '3.Exporter' block. Exclude addresses, roads, PIN, city.\n"
    "- 'invoice_numbers': Primary invoice number from '2.INV NO.' in SB21/SB22 (e.g. NF/156/2025-26, 132/2026, 3878). If multiple invoices, take the first/primary invoice only. Exclude dates, amounts, currency.\n"
    "- 'leo_date': Gatepass Order Date or LEO Date formatted as DD-Mon-YYYY (from '1.Order No / date' in Gatepass, or '6.LEO Date' in SB22).\n"
    "- 'container_numbers': Container number(s) from the 'Allowed for Movement' table in Gatepass (e.g. UNSU0088111). Join multiple with newline.\n"
    "- 'container_sizes': ISO container size(s) (20, 40, or 45) from Gatepass 'Allowed for Movement' table. Join multiple with newline.\n"
    "- 'customs_seal_numbers': Customs seal number(s) from Gatepass 'Allowed for Movement' table. Join multiple with newline. NEVER 'Pkg' or label words."
)


def _build_prompt(doc_type: str = "COMBINED") -> str:
    """Builds a targeted extraction prompt specialized by ICEGATE document type."""
    if doc_type == "COMBINED":
        return UNIFIED_PROMPT

    common_prefix = (
        "You are an expert Indian Customs ICEGATE document parser.\n"
        "Extract the required customs fields strictly from the document text provided below.\n"
        "Set any missing or inapplicable fields to null.\n\n"
    )

    if doc_type == "EGATEPASS":
        instructions = (
            "Document Type: Customs Gatepass (SB35)\n"
            "Target Fields:\n"
            "- 'shipper_name': TRUE exporter entity name ONLY (found after or under '4.CHA', e.g. NANDIKA FABRICS - NEVER the CHA company name or '3.Exporter' block). Exclude addresses, roads, PIN, city.\n"
            "- 'leo_date': Gatepass Order Date or LEO Date formatted as DD-Mon-YYYY (look under '1.Order No / date').\n"
            "- 'container_numbers': Container number(s) from the 'Allowed for Movement' table (e.g. TCKU7809338). Join multiple with newline.\n"
            "- 'container_sizes': ISO container size(s) (20, 40, or 45). Join multiple with newline.\n"
            "- 'customs_seal_numbers': Customs seal number(s) between container and size in 'Allowed for Movement' table. Join multiple with newline. NEVER 'Pkg' or label words.\n"
            "- 'invoice_numbers': null (not present on Gatepass)."
        )
    elif doc_type == "FINAL_LEO":
        instructions = (
            "Document Type: Final LEO Shipping Bill (SB22)\n"
            "Target Fields:\n"
            "- 'shipper_name': TRUE exporter entity name ONLY (the company line right after the discharge-port line like 'QAHMD (PORT HAMAD)' - NEVER the CHA block). Exclude addresses, roads, PIN, city.\n"
            "- 'invoice_numbers': Primary invoice number from '2.INV NO.' (e.g. NF/156/2025-26, 132/2026, 3878). Exclude dates, amounts and currency.\n"
            "- 'leo_date': LEO Date formatted as DD-Mon-YYYY (from '6.LEO Date' or '9.LEO').\n"
            "- 'container_numbers': null\n"
            "- 'container_sizes': null\n"
            "- 'customs_seal_numbers': null"
        )
    else:  # FIRST_COPY / default
        instructions = (
            "Document Type: First Copy Shipping Bill (SB21)\n"
            "Target Fields:\n"
            "- 'shipper_name': TRUE exporter entity name ONLY (the company line right after the discharge-port line like 'QAHMD (PORT HAMAD)' - NEVER the CHA block). Exclude addresses, roads, PIN, city.\n"
            "- 'invoice_numbers': Primary invoice number from '2.INV NO.' (e.g. NF/156/2025-26, 132/2026, 3878). Exclude dates, amounts and currency.\n"
            "- 'leo_date': null (not granted yet on First Copy)\n"
            "- 'container_numbers': null\n"
            "- 'container_sizes': null\n"
            "- 'customs_seal_numbers': null"
        )

    return common_prefix + instructions


def get_strict_json_schema() -> dict[str, Any]:
    """Generates an OpenAI/Meta-compliant strict JSON schema for CustomsExtractedFields."""
    schema = CustomsExtractedFields.model_json_schema()
    schema["required"] = list(schema.get("properties", {}).keys())
    schema["additionalProperties"] = False
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
            if e.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                sleep_s = 2 ** attempt
                logger.warning(f"OpenRouter HTTP {e.status_code} error, retrying in {sleep_s}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(sleep_s)
            else:
                logger.error(f"OpenRouter status error for SB {sb_no}: {e}")
                raise RuntimeError(f"OpenRouter AI extraction failed for SB {sb_no} ({doc_type}): {e}") from e
        except Exception as e:
            logger.error(f"OpenRouter unexpected error for SB {sb_no} ({doc_type}): {e}")
            raise RuntimeError(f"OpenRouter AI extraction failed for SB {sb_no} ({doc_type}): {e}") from e

    raw = response.choices[0].message.content or "{}" if response else "{}"
    try:
        parsed = CustomsExtractedFields.model_validate_json(raw)
    except ValidationError:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        parsed = CustomsExtractedFields.model_validate_json(cleaned)

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
