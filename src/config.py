import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (src/ -> root)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_EXCEL_PATH = BASE_DIR / "Daily Working Report.xlsx"
EXCEL_PATH = Path(os.getenv("EXCEL_PATH", str(DEFAULT_EXCEL_PATH)))
CACHE_DIR = Path(os.getenv("DAILY_REPORT_CACHE_DIR") or (BASE_DIR / ".cache" / "daily-report")).expanduser()

# OpenRouter Settings
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta/muse-spark-1.3-contributor")


def require_openrouter_key() -> str:
    """Ensures OPENROUTER_API_KEY is configured; raises RuntimeError otherwise."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing or empty.\n"
            "An OpenRouter API key is required to run document extraction.\n"
            "Please set OPENROUTER_API_KEY in your .env file: OPENROUTER_API_KEY=sk-or-v1-..."
        )
    return key


# Gmail Settings
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json")))
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json")))
GMAIL_MAX_THREADS = int(os.getenv("GMAIL_MAX_THREADS", "20"))

# Excel Column Aliases & Mapping
EXCEL_COLUMNS = {
    "sl_no": ["Sl No", "S.No", "Sl.No"],
    "fy": ["Financial Year", "FY"],
    "port_code": ["Port Code / ICD Code", "Port Code", "ICD Code"],
    "job_no": ["Job No", "Job No."],
    "job_date": ["Job Date", "Job Dt"],
    "inv_no": ["Inv No", "Invoice No", "Invoice Number", "Inv No.", "Invoice No."],
    "sb_no": ["Shipping Bill No", "Shipping Bill Number", "SB No", "SB Number"],
    "sb_date": ["Shipping Bill Date", "SB Date", "Shipping Bill Dt", "SB Dt"],
    "leo_date": ["LEO Date", "LEO Dt", "LEO Date.", "Let Export Date"],
    "shipper_name": ["Shipper Name", "Exporter Name", "Shipper", "Exporter", "Party Name"],
    "container_no": ["Container No.", "Container No", "Container Number", "Container", "Containers"],
    "container_size": ["Container Size", "Size", "Cont Size", "Cntr Size"],
    "customs_seal": ["Customs Seal Number", "Customs Seal No", "Customs Seal No.", "Customs Seal", "Seal Number", "Seal No"],
    "remarks": ["Remarks", "Remarks / Status", "Sync Status"],
    "billing_status": ["Billing Status", "Billing status", "Billing"],
    "status": ["Status"],
}

# Required sync targets
REQUIRED_DATA_FIELDS = [
    "inv_no",
    "leo_date",
    "shipper_name",
    "container_no",
    "container_size",
    "customs_seal",
]
