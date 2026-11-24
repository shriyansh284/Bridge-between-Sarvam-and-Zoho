"""
Thin client for the Sarvam endpoints this system needs. All calls go OUT from this
server, so no public address is needed (results are fetched, not received).

1. Instant Outbound  - place one call now, with per-candidate agent variables
   POST {outbound_base}/v1/orgs/{org}/workspaces/{ws}/outbounds
2. Analytics attempts - the result of our calls, looked up by attempt_id
   GET  {analytics_base}/v1/{org}/{ws}/{app_id}/attempts
3. Analytics transcript - what was said in a connected call
   GET  {analytics_base}/v1/{org}/{ws}/{app_id}/transcripts/{interaction_id}
4. Analytics recording - download the WAV for an interaction, server-side (dashboard)
   GET  {analytics_base}/v1/{org}/{ws}/{app_id}/recordings/{interaction_id}

All use the workspace API key (X-API-Key). Your dashboard email/password is
never used anywhere. If PUBLIC_BASE_URL is set, Sarvam is also asked to post each
result to our webhook (faster); the fetch above still runs as a safety net.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from .config import settings
from .timeutil import utcnow

log = logging.getLogger(__name__)
TIMEOUT = 30


class SarvamError(Exception):
    """retryable: safe to try again. uncertain: Sarvam may have acted on the request although we got
    no answer (e.g. a read timeout after sending), so placing the call again could call someone twice."""

    def __init__(self, message: str, retryable: bool, status_code: Optional[int] = None, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.uncertain = uncertain


def _never_sent(exc: Exception) -> bool:
    """True when the request certainly never reached Sarvam (DNS failure, connection refused)."""
    text = repr(exc)
    return any(s in text for s in ("NewConnectionError", "NameResolutionError", "Failed to resolve",
                                   "Name or service not known", "getaddrinfo failed", "Connection refused"))


def _headers() -> dict:
    return {"X-API-Key": settings.sarvam_api_key, "Content-Type": "application/json"}


def format_phone_for_sarvam(e164: str) -> str:
    fmt = settings.sarvam_phone_format.lower()
    if fmt == "digits":
        return e164.lstrip("+")
    if fmt == "national":
        return e164[-10:]
    return e164


def webhooks_enabled() -> bool:
    """Only when this server has a public address can Sarvam post results to it."""
    return bool(settings.public_base_url and settings.webhook_secret)


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
    payload = {
        "app_config": app_config,
        "user_config": {"user_phone_number": format_phone_for_sarvam(phone_e164)},
    }
    if webhooks_enabled():
        payload["webhook_config"] = {"url": settings.webhook_url, "metadata": metadata}
    return payload


def _analytics_url(path: str) -> str:
    return (f"{settings.sarvam_analytics_base.rstrip('/')}/v1/{settings.sarvam_org_id}/"
            f"{settings.sarvam_workspace_id}/{settings.sarvam_app_id}/{path}")


ANALYTICS_TIMEOUT = 15  # short: these lookups run inside the loop that also places calls and sends emails


def _analytics_get(path: str, params: Optional[dict] = None, timeout: int = ANALYTICS_TIMEOUT) -> dict:
    try:
        resp = requests.get(_analytics_url(path), headers=_headers(), params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SarvamError(f"Network error reaching Sarvam: {exc}", retryable=True) from exc
    if resp.status_code >= 400:
        raise SarvamError(f"Sarvam API {resp.status_code}: {resp.text[:300]}",
                          resp.status_code in (408, 429) or resp.status_code >= 500, resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise SarvamError(f"Unexpected Sarvam response: {resp.text[:300]}", retryable=True) from exc


ATTEMPT_BATCH = 20  # attempt IDs per lookup: keeps the request URL around 3 KB


def fetch_attempts(attempt_ids: list[str], since: datetime) -> list[dict]:
    """Analytics records for some of our own calls (at most ATTEMPT_BATCH), found by the attempt_id Sarvam
    returned when each call was placed. `since` (naive UTC) bounds the search."""
    end = (utcnow() + timedelta(minutes=10)).isoformat()
    conditions = [{"field": "attempt_id", "operator": "equals", "value": str(a)} for a in attempt_ids]
    found: list[dict] = []
    offset = 0
    while True:
        data = _analytics_get("attempts", {
            "start_datetime": since.isoformat(), "end_datetime": end, "limit": 1000, "offset": offset,
            "filter_conditions": json.dumps(conditions)})
        items = data.get("items") or []
        found.extend(items)
        if len(items) < 1000 or offset >= 9000:
            break
        offset += 1000
    return found


def fetch_transcript(interaction_id: str) -> list[dict]:
    """[{role: agent|user, text}] for one conversation; [] when Sarvam has none."""
    data = _analytics_get(f"transcripts/{quote(interaction_id, safe='')}")
    turns = sorted(data.get("messages") or [], key=lambda m: m.get("turn_id") or 0)
    return [{"role": "agent" if str(m.get("role", "")).lower() in ("assistant", "agent") else "user",
             "text": m.get("content") or "", "language": m.get("language_name")} for m in turns]


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
    except requests.ConnectTimeout as exc:  # never connected: nothing was sent
        raise SarvamError(f"Couldn't connect to Sarvam: {exc}", retryable=True) from exc
    except requests.ConnectionError as exc:
        if _never_sent(exc):
            raise SarvamError(f"Couldn't connect to Sarvam: {exc}", retryable=True) from exc
        raise SarvamError(f"Connection to Sarvam dropped after sending: {exc}", retryable=False, uncertain=True) from exc
    except requests.Timeout as exc:  # sent, but no answer in time: the call may be going out
        raise SarvamError(f"Sarvam didn't answer in {TIMEOUT} s: {exc}", retryable=False, uncertain=True) from exc
    except requests.RequestException as exc:
        raise SarvamError(f"Error calling Sarvam: {exc}", retryable=False, uncertain=True) from exc

    if 200 <= resp.status_code < 300:
        try:
            attempt_id = resp.json()["attempt_id"]
        except (ValueError, KeyError) as exc:  # accepted, but we can't tell which call it is
            raise SarvamError(f"Unexpected Sarvam response: {resp.text[:300]}", retryable=False, uncertain=True) from exc
        return str(attempt_id)

    code = resp.status_code
    if code in (500, 502, 504):  # the request may have been processed before the error
        raise SarvamError(f"Sarvam API {code}: {resp.text[:500]}", retryable=False, status_code=code, uncertain=True)
    retryable = code in (408, 409, 425, 429, 503) or code > 504
    raise SarvamError(f"Sarvam API {code}: {resp.text[:500]}", retryable, code)


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
