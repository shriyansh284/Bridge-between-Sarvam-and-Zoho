"""
Zoho Mail REST client (Self Client OAuth, refresh-token flow).

Scopes to tick when generating the grant code in api-console.zoho.in:
    ZohoMail.messages.ALL,ZohoMail.accounts.READ,ZohoMail.folders.READ

Endpoints used (all under {ZOHO_MAIL_API_URL}/api/accounts/{accountId}):
    GET  /messages/view?folderId&status=unread&attachedMails=true   new emails with attachments
    GET  /folders/{f}/messages/{m}/attachmentinfo                    attachment list
    GET  /folders/{f}/messages/{m}/attachments/{a}                   attachment bytes
    POST /messages/attachments?uploadType=multipart                  upload result file
    POST /messages/{m}            action=reply                       reply in the same thread
    POST /messages                                                   new email (dashboard uploads)
    PUT  /updatemessage           mode=markAsRead
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

import requests

from . import email_settings
from .config import settings

log = logging.getLogger(__name__)
TIMEOUT = 60
EMAIL_FIND = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _addresses(account: dict) -> set[str]:
    """Every address that appears in a Zoho account record (primary, aliases, send-as)."""
    return {a.lower() for a in EMAIL_FIND.findall(json.dumps(account, default=str))}


class ZohoError(Exception):
    pass


class ZohoMail:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._account_id: Optional[str] = None
        self._account_for: Optional[str] = None   # mailbox address the cached account was chosen for
        self._folder_id: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ auth
    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and time.time() < self._token_expiry - 60:
                return self._token
            resp = requests.post(
                f"{settings.zoho_accounts_url.rstrip('/')}/oauth/v2/token",
                params={"refresh_token": settings.zoho_refresh_token, "client_id": settings.zoho_client_id,
                        "client_secret": settings.zoho_client_secret, "grant_type": "refresh_token"},
                timeout=TIMEOUT)
            data = resp.json() if resp.content else {}
            if resp.status_code != 200 or "access_token" not in data:
                raise ZohoError(f"Zoho token refresh failed ({resp.status_code}): {data or resp.text[:200]}. "
                                "Check ZOHO_DC, client id/secret and refresh token.")
            self._token = data["access_token"]
            self._token_expiry = time.time() + int(data.get("expires_in", 3600))
            return self._token

    def _request(self, method: str, path: str, *, expect_json: bool = True, **kwargs):
        url = f"{settings.zoho_mail_api_url.rstrip('/')}/api{path}"
        for attempt in (1, 2):
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Zoho-oauthtoken {self._access_token(force=attempt == 2)}"
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
            if resp.status_code == 401 and attempt == 1:
                kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            if resp.status_code >= 400:
                raise ZohoError(f"Zoho {method} {path} -> {resp.status_code}: {resp.text[:300]}")
            if not expect_json:
                return resp.content
            body = resp.json()
            code = (body.get("status") or {}).get("code", 200)
            if code != 200:
                raise ZohoError(f"Zoho {method} {path} -> {body}")
            return body.get("data")
        raise ZohoError("Zoho authentication failed twice")

    # ------------------------------------------------------------ discovery
    def account_id(self) -> str:
        want = email_settings.mailbox().lower()
        fixed = settings.zoho_account_id  # ZOHO_ACCOUNT_ID pins the account whatever the mailbox address
        if self._account_id and (fixed or self._account_for == want):
            return self._account_id
        if fixed:
            chosen_id = fixed
        else:
            accounts = self._request("GET", "/accounts") or []
            if not accounts:
                raise ZohoError("No Zoho Mail account found for this token")
            chosen = next((a for a in accounts if want in _addresses(a)), None)
            if chosen is None:
                chosen = accounts[0]
                log.warning("Mailbox %s isn't one of this Zoho login's accounts; using %s", want,
                            chosen.get("primaryEmailAddress") or chosen["accountId"])
            chosen_id = str(chosen["accountId"])
        if chosen_id != self._account_id:
            self._folder_id = None  # folder ids belong to one account
        self._account_id, self._account_for = chosen_id, want
        return self._account_id

    def check_mailbox(self, address: str) -> tuple[bool, str]:
        """Is this address one this Zoho login can read and send as? Reads only."""
        address = address.strip().lower()
        accounts = self._request("GET", "/accounts") or []
        for acc in accounts:
            if address in _addresses(acc):
                return True, f"{address} is in Zoho account {acc['accountId']}. Replies will be sent from it."
        owned = sorted({str(a.get("primaryEmailAddress") or a.get("mailboxAddress") or a["accountId"])
                        for a in accounts})
        return False, (f"{address} isn't a mailbox or alias of this Zoho login (it has: {', '.join(owned) or 'none'}). "
                       "Add it as an alias in Zoho, or create a new ZOHO_REFRESH_TOKEN signed in as that mailbox "
                       "(README step 3).")

    def folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        folders = self._request("GET", f"/accounts/{self.account_id()}/folders") or []
        name = settings.zoho_folder.strip().lower()
        chosen = next((f for f in folders if str(f.get("folderName", "")).strip().lower() == name), None)
        if chosen is None and name == "inbox":
            chosen = next((f for f in folders if str(f.get("folderType", "")).lower() == "inbox"), None)
        if not chosen:  # never fall back to another folder: it may hold mail meant for other agents
            names = ", ".join(sorted(str(f.get("folderName")) for f in folders))
            raise ZohoError(f"Folder '{settings.zoho_folder}' not found in Zoho Mail. Check ZOHO_FOLDER "
                            f"(spelling and spaces). Folders in this mailbox: {names}")
        self._folder_id = str(chosen["folderId"])
        return self._folder_id

    # ------------------------------------------------------------ reading
    def unread_with_attachments(self, limit: int = 100) -> list[dict]:
        params = {"folderId": self.folder_id(), "status": "unread", "attachedMails": "true",
                  "limit": limit, "sortorder": "true"}
        return self._request("GET", f"/accounts/{self.account_id()}/messages/view", params=params) or []

    def attachments(self, folder_id: str, message_id: str) -> list[dict]:
        data = self._request("GET", f"/accounts/{self.account_id()}/folders/{folder_id}/messages/{message_id}/attachmentinfo")
        return (data or {}).get("attachments", [])

    def download(self, folder_id: str, message_id: str, attachment_id: str) -> bytes:
        return self._request(
            "GET", f"/accounts/{self.account_id()}/folders/{folder_id}/messages/{message_id}/attachments/{attachment_id}",
            expect_json=False, headers={"Accept": "application/octet-stream"})

    def mark_read(self, message_ids: list[str]) -> None:
        self._request("PUT", f"/accounts/{self.account_id()}/updatemessage",
                      json={"mode": "markAsRead", "messageId": [int(m) for m in message_ids]})

    # ------------------------------------------------------------ sending
    def upload(self, filename: str, data: bytes) -> dict:
        result = self._request(
            "POST", f"/accounts/{self.account_id()}/messages/attachments",
            params={"uploadType": "multipart", "isInline": "false"},
            files={"attach": (filename, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        item = result[0] if isinstance(result, list) else result
        return {k: item[k] for k in ("storeName", "attachmentPath", "attachmentName")}

    def _body(self, to: str, subject: str, html: str, attachments: list[dict], cc: str = "") -> dict:
        body = {"fromAddress": email_settings.mailbox(), "toAddress": to, "subject": subject,
                "content": html, "mailFormat": "html"}
        if cc:
            body["ccAddress"] = cc
        if attachments:
            body["attachments"] = attachments
        return body

    def reply(self, message_id: str, to: str, subject: str, html: str,
              attachments: Optional[list[dict]] = None, cc: str = "") -> None:
        body = self._body(to, subject, html, attachments or [], cc)
        body["action"] = "reply"
        self._request("POST", f"/accounts/{self.account_id()}/messages/{message_id}", json=body)

    def send(self, to: str, subject: str, html: str, attachments: Optional[list[dict]] = None, cc: str = "") -> None:
        self._request("POST", f"/accounts/{self.account_id()}/messages",
                      json=self._body(to, subject, html, attachments or [], cc))


_client: Optional[ZohoMail] = None


def client() -> ZohoMail:
    global _client
    if _client is None:
        _client = ZohoMail()
    return _client


def extract_email(text: str) -> str:
    text = (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    found = EMAIL_FIND.findall(text)
    return found[-1].lower() if found else text.strip().lower()


def sender_allowed(email: str) -> bool:
    allowed = email_settings.allowed_senders()
    if not allowed:
        return True
    email = (email or "").lower()
    domain = "@" + email.split("@")[-1]
    return email in allowed or domain in allowed
