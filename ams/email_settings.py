"""
Email addresses the team can change from the dashboard (Email settings tab),
for when your own address or the client's address is replaced. No .env edit
and no restart: the backend re-reads this file every time it needs an address.

    mailbox          the fixed AMS address clients send files to (also the From of every reply)
    allowed_senders  client emails or @domains allowed to start campaigns
    cc               internal copy of every reply (optional)

Stored in data/state/email_settings.json. A value saved here overrides .env;
resetting it removes the override so the .env value applies again. Every change
is logged to data/state/settings_log.jsonl.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import settings
from .store import STATE, append_log, lock, read_json, read_log, write_json
from .timeutil import iso, utcnow

PATH = STATE / "email_settings.json"
KEYS = ("mailbox", "allowed_senders", "cc")
EMAIL_RE = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")
DOMAIN_RE = re.compile(r"^@[\w-]+(?:\.[\w-]+)+$")


def _saved() -> dict:
    return read_json(PATH, {}) or {}


def mailbox() -> str:
    saved = _saved()
    return saved["mailbox"] if saved.get("mailbox") else settings.zoho_from_address


def allowed_senders() -> tuple:
    saved = _saved()
    return tuple(saved["allowed_senders"]) if "allowed_senders" in saved else settings.allowed_senders


def cc() -> str:
    saved = _saved()
    return ", ".join(saved["cc"]) if "cc" in saved else settings.cc_addresses


def source(key: str) -> str:
    """'dashboard' when the value was changed in the dashboard, '.env' otherwise."""
    return "dashboard" if key in _saved() else ".env"


def is_email(text: str) -> bool:
    return bool(EMAIL_RE.match((text or "").strip()))


def parse_addresses(text: str, allow_domains: bool = False) -> tuple[list[str], list[str]]:
    """Split on commas, semicolons, spaces or new lines. Returns (valid, invalid), lower-cased, no repeats."""
    good, bad = [], []
    for item in re.split(r"[,;\s]+", text or ""):
        item = item.strip().lower()
        if not item:
            continue
        if EMAIL_RE.match(item) or (allow_domains and DOMAIN_RE.match(item)):
            if item not in good:
                good.append(item)
        else:
            bad.append(item)
    return good, bad


def save(changes: dict, user: str) -> None:
    """changes: {key: new value}; a value of None removes the override (back to .env)."""
    unknown = set(changes) - set(KEYS)
    if unknown:
        raise ValueError(f"Unknown email setting(s): {', '.join(sorted(unknown))}")
    with lock("email-settings"):
        data = _saved()
        before = {k: data.get(k) for k in changes}
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        data["updated_at"], data["updated_by"] = iso(utcnow()), user
        write_json(PATH, data)
    append_log("settings_log", {"by": user, "changes": changes, "before": before})


def history(limit: int = 20) -> list[dict]:
    return read_log("settings_log", limit)


def last_change() -> Optional[dict]:
    saved = _saved()
    return {"at": saved["updated_at"], "by": saved.get("updated_by")} if saved.get("updated_at") else None
