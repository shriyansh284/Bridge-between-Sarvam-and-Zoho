"""
All settings come from environment variables or a .env file in the project root.
Secrets (Sarvam API key, Zoho client secret / refresh token) live only here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    if not os.environ.get("AMS_NO_DOTENV"):  # the tests set this so the real .env never leaks in
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _get(name: str, default=None):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int(name: str, default: int) -> int:
    return int(_get(name, default))


def _bool(name: str, default: bool) -> bool:
    return str(_get(name, default)).lower() in ("1", "true", "yes", "on")


def _time(name: str, default: str) -> time:
    h, m = str(_get(name, default)).split(":")
    return time(int(h), int(m))


def _list(name: str, default: str = "") -> tuple:
    return tuple(x.strip().lower() for x in str(_get(name, default)).split(",") if x.strip())


# Sarvam agent variable -> field read from the client's sheet.
# Left side must match the variable names defined in your Sarvam agent.
DEFAULT_AGENT_VARIABLE_MAP = {
    "name": "candidate_name",
    "phone_number": "phone_national",          # 10 digits, as in the cohort CSV
    "Name_of_Hiring_company": "hiring_company",
    "pending_document_names": "pending_documents",
    "whatsapp_message_date": "whatsapp_message_date",
}

# Result column in the Excel -> key in the agent's final_agent_variables.
# Matches agent Conversatio-3f6c465d-6251 (checked with `python -m ams.sarvam_setup check`).
DEFAULT_RESULT_FIELD_MAP = {
    "Candidate Response": "disposition_call_disposition",
    "Commitment & Timeline": "disposition_commitment_with_timeline",
    "Call Summary": "summary",
    "Call Quality": "call_conversation_quality",
}

_DC = {
    "in": ("https://accounts.zoho.in", "https://mail.zoho.in"),
    "com": ("https://accounts.zoho.com", "https://mail.zoho.com"),
    "eu": ("https://accounts.zoho.eu", "https://mail.zoho.eu"),
    "com.au": ("https://accounts.zoho.com.au", "https://mail.zoho.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://mail.zoho.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://mail.zohocloud.ca"),
    "sa": ("https://accounts.zoho.sa", "https://mail.zoho.sa"),
}
_zoho_dc = _get("ZOHO_DC", "in")


@dataclass(frozen=True)
class Settings:
    # --- Sarvam ---------------------------------------------------------
    sarvam_api_key: str = _get("SARVAM_API_KEY", "")
    sarvam_org_id: str = _get("SARVAM_ORG_ID", "")
    sarvam_workspace_id: str = _get("SARVAM_WORKSPACE_ID", "")
    sarvam_app_id: str = _get("SARVAM_APP_ID", "")
    sarvam_app_version: int = _int("SARVAM_APP_VERSION", 1)
    sarvam_connection_id: str = _get("SARVAM_CONNECTION_ID", "")
    sarvam_agent_phone_number: str = _get("SARVAM_AGENT_PHONE_NUMBER", "")
    sarvam_outbound_base: str = _get("SARVAM_OUTBOUND_BASE", "https://apps.sarvam.ai/api/outbounds")
    sarvam_analytics_base: str = _get("SARVAM_ANALYTICS_BASE", "https://apps.sarvam.ai/api/analytics")
    sarvam_phone_format: str = _get("SARVAM_PHONE_FORMAT", "e164")  # e164 | digits | national

    # --- Zoho Mail ------------------------------------------------------
    zoho_client_id: str = _get("ZOHO_CLIENT_ID", "")
    zoho_client_secret: str = _get("ZOHO_CLIENT_SECRET", "")
    zoho_refresh_token: str = _get("ZOHO_REFRESH_TOKEN", "")
    zoho_redirect_uri: str = _get("ZOHO_REDIRECT_URI", "")      # only for a Server-based client (blank = Self Client)
    zoho_accounts_url: str = _get("ZOHO_ACCOUNTS_URL", _DC.get(_zoho_dc, _DC["in"])[0])
    zoho_mail_api_url: str = _get("ZOHO_MAIL_API_URL", _DC.get(_zoho_dc, _DC["in"])[1])
    zoho_account_id: str = _get("ZOHO_ACCOUNT_ID", "")         # blank = discover automatically
    # ZOHO_FROM_ADDRESS, ALLOWED_SENDERS and REPLY_CC are starting values; the dashboard's
    # Email settings tab can change them later (read them through ams/email_settings.py)
    zoho_from_address: str = _get("ZOHO_FROM_ADDRESS", "")     # the fixed address clients write to
    zoho_folder: str = _get("ZOHO_FOLDER", "Inbox")
    mail_poll_seconds: int = _int("MAIL_POLL_SECONDS", 60)
    mail_enabled: bool = _bool("MAIL_ENABLED", True)
    allowed_senders: tuple = _list("ALLOWED_SENDERS")           # emails or @domain.com; blank = anyone
    cc_addresses: str = _get("REPLY_CC", "")                    # internal copy of every reply
    send_ack: bool = _bool("SEND_ACK", True)
    duplicate_file_days: int = _int("DUPLICATE_FILE_DAYS", 7)

    # --- Public URLs (OPTIONAL) -----------------------------------------
    # Without a public address the app fetches each call's result from Sarvam every RESULT_POLL_SECONDS,
    # and recording links in the Excel point at Sarvam's own signed links. With one, Sarvam also posts
    # results to our webhook straight away and recording links go through this server.
    public_base_url: str = _get("PUBLIC_BASE_URL", "")          # where server.py is reachable over HTTPS
    webhook_secret: str = _get("WEBHOOK_SECRET", "")
    link_secret: str = _get("LINK_SECRET", "") or _get("WEBHOOK_SECRET", "")
    recording_link_days: int = _int("RECORDING_LINK_DAYS", 30)

    # --- Storage (files, no database) -----------------------------------
    data_dir: Path = Path(_get("DATA_DIR", str(BASE_DIR / "data")))

    # --- Calling rules --------------------------------------------------
    timezone: str = _get("TIMEZONE", "Asia/Kolkata")
    call_window_start: time = _time("CALL_WINDOW_START", "09:00")
    call_window_end: time = _time("CALL_WINDOW_END", "21:00")
    call_days: tuple = tuple(int(d) for d in str(_get("CALL_DAYS", "0,1,2,3,4,5,6")).split(","))
    blank_time: str = _get("BLANK_TIME", "now")                 # now | reject
    max_attempts: int = _int("MAX_ATTEMPTS", 2)
    retry_gap_minutes: int = _int("RETRY_GAP_MINUTES", 60)
    max_trigger_errors: int = _int("MAX_TRIGGER_ERRORS", 3)
    trigger_error_backoff_minutes: int = _int("TRIGGER_ERROR_BACKOFF_MINUTES", 5)
    result_timeout_minutes: int = _int("RESULT_TIMEOUT_MINUTES", 45)
    result_poll_seconds: int = _int("RESULT_POLL_SECONDS", 60)   # how often finished calls are fetched from Sarvam
    max_calls_per_cycle: int = _int("MAX_CALLS_PER_CYCLE", 5)
    scheduler_poll_seconds: int = _int("SCHEDULER_POLL_SECONDS", 20)
    past_time_grace_hours: int = _int("PAST_TIME_GRACE_HOURS", 24)

    # --- Result emails --------------------------------------------------
    batch_mode: str = _get("BATCH_MODE", "slot")    # slot | size | final
    batch_size: int = _int("BATCH_SIZE", 10)
    max_email_failures: int = _int("MAX_EMAIL_FAILURES", 5)

    # --- Mapping --------------------------------------------------------
    agent_variable_map: dict = field(
        default_factory=lambda: json.loads(_get("AGENT_VARIABLE_MAP", "")) if _get("AGENT_VARIABLE_MAP")
        else dict(DEFAULT_AGENT_VARIABLE_MAP))
    result_field_map: dict = field(
        default_factory=lambda: json.loads(_get("RESULT_FIELD_MAP", "")) if _get("RESULT_FIELD_MAP")
        else dict(DEFAULT_RESULT_FIELD_MAP))

    # --- Dashboard ------------------------------------------------------
    dashboard_users_file: str = _get("DASHBOARD_USERS_FILE", str(BASE_DIR / "config" / "users.json"))
    dashboard_users_json: str = _get("DASHBOARD_USERS", "")

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/webhooks/sarvam/{self.webhook_secret}"

    def missing_for_calling(self) -> list[str]:
        need = {
            "SARVAM_API_KEY": self.sarvam_api_key, "SARVAM_ORG_ID": self.sarvam_org_id,
            "SARVAM_WORKSPACE_ID": self.sarvam_workspace_id, "SARVAM_APP_ID": self.sarvam_app_id,
            "SARVAM_CONNECTION_ID": self.sarvam_connection_id,
            "SARVAM_AGENT_PHONE_NUMBER": self.sarvam_agent_phone_number,
        }  # PUBLIC_BASE_URL is optional: results are fetched from Sarvam when there is none
        return [k for k, v in need.items() if not v]

    def missing_for_mail(self) -> list[str]:
        from .email_settings import mailbox  # the address can also be set in the dashboard

        need = {
            "ZOHO_CLIENT_ID": self.zoho_client_id, "ZOHO_CLIENT_SECRET": self.zoho_client_secret,
            "ZOHO_REFRESH_TOKEN": self.zoho_refresh_token, "ZOHO_FROM_ADDRESS": mailbox(),
        }
        return [k for k, v in need.items() if not v]

    def dashboard_users(self) -> dict:
        if self.dashboard_users_json:
            return json.loads(self.dashboard_users_json)
        path = Path(self.dashboard_users_file)
        return json.loads(path.read_text()) if path.exists() else {}


settings = Settings()
