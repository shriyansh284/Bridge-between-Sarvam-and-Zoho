"""
Thin client for the two Sarvam endpoints this system needs.

1. Instant Outbound  - place one call now, with per-candidate agent variables
   POST {outbound_base}/v1/orgs/{org}/workspaces/{ws}/outbounds
2. Analytics recording - download the WAV for an interaction, server-side
   GET  {analytics_base}/v1/{org}/{ws}/{app_id}/recordings/{interaction_id}

Both use the workspace API key (X-API-Key). Your dashboard email/password is
never used anywhere.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from .config import settings

log = logging.getLogger(__name__)
TIMEOUT = 30


class SarvamError(Exception):
    def __init__(self, message: str, retryable: bool, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _headers() -> dict:
    return {"X-API-Key": settings.sarvam_api_key, "Content-Type": "application/json"}


def format_phone_for_sarvam(e164: str) -> str:
    fmt = settings.sarvam_phone_format.lower()
    if fmt == "digits":
        return e164.lstrip("+")
    if fmt == "national":
        return e164[-10:]
    return e164


def build_outbound_payload(phone_e164: str, agent_variables: dict, app_overrides: Optional[dict],
                           metadata: dict) -> dict:
    app_config: dict = {
        "app_id": settings.sarvam_app_id,
        "app_version": settings.sarvam_app_version,
        "connection_config": {
            "connection_id": settings.sarvam_connection_id,
            "agent_phone_number": settings.sarvam_agent_phone_number,
        },
        "agent_variables": agent_variables or {},
    }
    overrides = {k: v for k, v in (app_overrides or {}).items() if v}
    if overrides:
        app_config["app_overrides"] = overrides
    return {
        "app_config": app_config,
        "user_config": {"user_phone_number": format_phone_for_sarvam(phone_e164)},
        "webhook_config": {"url": settings.webhook_url, "metadata": metadata},
    }


def place_call(phone_e164: str, agent_variables: dict, app_overrides: Optional[dict], metadata: dict) -> str:
    """Place one outbound call. Returns Sarvam's attempt_id. Raises SarvamError."""
    missing = settings.missing_for_calling()
    if missing:
        raise SarvamError(f"Missing settings: {', '.join(missing)}", retryable=False)

    url = (f"{settings.sarvam_outbound_base.rstrip('/')}/v1/orgs/{settings.sarvam_org_id}"
           f"/workspaces/{settings.sarvam_workspace_id}/outbounds")
    payload = build_outbound_payload(phone_e164, agent_variables, app_overrides, metadata)
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SarvamError(f"Network error reaching Sarvam: {exc}", retryable=True) from exc

    if 200 <= resp.status_code < 300:
        try:
            attempt_id = resp.json()["attempt_id"]
        except (ValueError, KeyError) as exc:
            raise SarvamError(f"Unexpected Sarvam response: {resp.text[:300]}", retryable=False) from exc
        return str(attempt_id)

    retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
    raise SarvamError(f"Sarvam API {resp.status_code}: {resp.text[:500]}", retryable, resp.status_code)


def fetch_recording(interaction_id: str, app_id: Optional[str] = None) -> Optional[bytes]:
    """Download the call recording (WAV). Returns None if Sarvam has no recording."""
    if not interaction_id:
        return None
    url = (f"{settings.sarvam_analytics_base.rstrip('/')}/v1/{settings.sarvam_org_id}/"
           f"{settings.sarvam_workspace_id}/{app_id or settings.sarvam_app_id}/recordings/"
           f"{quote(interaction_id, safe='')}")
    resp = requests.get(url, headers={"X-API-Key": settings.sarvam_api_key}, timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def fetch_recording_url(recording_url: str) -> Optional[bytes]:
    """For payloads that carry a recording_url (campaign webhooks). The API key is only
    ever sent to Sarvam's own host, never to any other domain."""
    host = urlparse(recording_url).hostname or ""
    headers = {"X-API-Key": settings.sarvam_api_key} if host.endswith("sarvam.ai") else {}
    resp = requests.get(recording_url, headers=headers, timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content
