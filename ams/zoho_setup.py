"""
Zoho Mail setup helper. Get a refresh token one of two ways, then check.

A) Self Client (api-console.zoho.in -> Self Client -> Generate Code), scope:
       ZohoMail.messages.ALL,ZohoMail.accounts.READ,ZohoMail.folders.READ
   (time duration: 10 minutes), then within those minutes run:

       python -m ams.zoho_setup token <GRANT_CODE>

B) Server-based client (has an Authorized Redirect URI): set ZOHO_REDIRECT_URI in .env
   to exactly that URI, then

       python -m ams.zoho_setup url

   open the printed link signed in as the AMS mailbox, accept, and copy the whole
   address the browser lands on (it holds ?code=..., even if the page doesn't load).
   Within 2 minutes run:

       python -m ams.zoho_setup token "<THE WHOLE ADDRESS>"

Either way, put the printed refresh token in .env as ZOHO_REFRESH_TOKEN, then
check everything works (reads only, sends nothing):

       python -m ams.zoho_setup check
"""
from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import settings
from .zoho import ZohoError, client

SCOPES = "ZohoMail.messages.ALL,ZohoMail.accounts.READ,ZohoMail.folders.READ"


def auth_url() -> None:
    if not (settings.zoho_client_id and settings.zoho_redirect_uri):
        print("Set ZOHO_CLIENT_ID and ZOHO_REDIRECT_URI in .env first (the redirect URI exactly as in the Zoho console).")
        sys.exit(1)
    query = urlencode({"response_type": "code", "client_id": settings.zoho_client_id, "scope": SCOPES,
                       "redirect_uri": settings.zoho_redirect_uri, "access_type": "offline", "prompt": "consent"})
    print("Open this link signed in as the AMS mailbox, accept, then copy the whole address you land on:\n")
    print(f"{settings.zoho_accounts_url.rstrip('/')}/oauth/v2/auth?{query}")


def exchange(code_or_url: str) -> None:
    code, accounts = code_or_url.strip(), settings.zoho_accounts_url.rstrip("/")
    if code.startswith("http"):  # the whole redirect address was pasted
        params = parse_qs(urlparse(code).query)
        if "error" in params:
            print(f"Zoho returned an error: {params['error'][0]}")
            sys.exit(1)
        if "code" not in params:
            print("That address has no ?code=... in it. Paste the address the browser landed on after accepting.")
            sys.exit(1)
        server = params.get("accounts-server", [""])[0].rstrip("/")
        if server and server != accounts:
            print(f"Note: this Zoho account lives on {server}; set ZOHO_DC to match "
                  f"(e.g. {server.split('accounts.zoho')[-1].lstrip('.') or 'com'}).")
            accounts = server
        code = params["code"][0]
    body = {"code": code, "grant_type": "authorization_code",
            "client_id": settings.zoho_client_id, "client_secret": settings.zoho_client_secret}
    if settings.zoho_redirect_uri:
        body["redirect_uri"] = settings.zoho_redirect_uri
    resp = requests.post(f"{accounts}/oauth/v2/token", params=body, timeout=30)
    data = resp.json()
    if "refresh_token" not in data:
        print(f"Failed: {data}\nCheck ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_DC / ZOHO_REDIRECT_URI, and that "
              "the code is fresh (Self Client: 10 minutes; Server-based: about 2 minutes, single use).")
        sys.exit(1)
    print("Add this line to .env:\n")
    print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}")


def check() -> None:
    missing = settings.missing_for_mail()
    if missing:
        print("Fill these in .env first:", ", ".join(missing))
        sys.exit(1)
    try:
        zm = client()
        print("Account ID:     ", zm.account_id(), "(you can set ZOHO_ACCOUNT_ID to this)")
        print("Folder ID:      ", zm.folder_id(), f"({settings.zoho_folder})")
        unread = zm.unread_with_attachments(limit=50)
        print("Unread with attachments:", len(unread))
        for m in unread[:5]:
            print("   ", m.get("fromAddress"), "|", m.get("subject"))
        print("\nZoho Mail is connected.")
    except ZohoError as exc:
        print("Zoho error:", exc)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "token":
        exchange(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "url":
        auth_url()
    elif len(sys.argv) == 2 and sys.argv[1] == "check":
        check()
    else:
        print(__doc__)
