"""
Signed recording links for the result Excel.

A link looks like  https://calls.yourdomain.com/rec/<token>
The token names one call attempt and an expiry date, signed with LINK_SECRET.
Anyone holding the link can play that one recording until it expires;
nobody ever sees the Sarvam login or API key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

from .config import settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(settings.link_secret.encode(), body.encode(), hashlib.sha256).digest()[:18])


def make_token(campaign_id: str, row_id: str, attempt_ref: str, days: Optional[int] = None) -> str:
    expires = int(time.time()) + 86400 * (days or settings.recording_link_days)
    body = _b64(f"{campaign_id}|{row_id}|{attempt_ref}|{expires}".encode())
    return f"{body}.{_sign(body)}"


def read_token(token: str) -> Optional[tuple[str, str, str]]:
    try:
        body, sig = token.split(".", 1)
        if not settings.link_secret or not hmac.compare_digest(sig, _sign(body)):
            return None
        cid, row_id, ref, expires = _unb64(body).decode().split("|")
        if int(expires) < time.time():
            return None
        return cid, row_id, ref
    except (ValueError, UnicodeDecodeError):
        return None


def recording_link(campaign_id: str, row_id: str, attempt_ref: str) -> str:
    if not settings.public_base_url or not settings.link_secret:
        return ""
    return f"{settings.public_base_url.rstrip('/')}/rec/{make_token(campaign_id, row_id, attempt_ref)}"
