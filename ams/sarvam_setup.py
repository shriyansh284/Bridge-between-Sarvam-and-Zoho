"""
Sarvam setup helper. Reads only; it never places a call.

    python -m ams.sarvam_setup check

1. Checks SARVAM_API_KEY, SARVAM_ORG_ID and SARVAM_WORKSPACE_ID together.
2. Finds the telephony connection and agent phone number used by your recent
   campaigns for this agent (SARVAM_CONNECTION_ID / SARVAM_AGENT_PHONE_NUMBER).
3. Lists the agent versions and the agent variable names seen in recent calls, and
   compares them with AGENT_VARIABLE_MAP / RESULT_FIELD_MAP. Only names are printed,
   never values (they can contain candidates' personal data).

The key must be a Voice Agents workspace key (Voice Agents dashboard -> Settings -> API Key),
sent as X-API-Key. Keys from the Sarvam API platform (indus.sarvam.ai, used for the speech and
text APIs and by sarvam-mcp) are a different kind and are rejected here.
"""
from __future__ import annotations

import sys
from datetime import timedelta

import requests

from .config import settings
from .timeutil import utcnow

TIMEOUT = 40
ROOT = settings.sarvam_outbound_base.split("/api/")[0]  # https://apps.sarvam.ai


def _get(url: str, **params):
    resp = requests.get(url, headers={"X-API-Key": settings.sarvam_api_key}, params=params, timeout=TIMEOUT)
    if resp.status_code == 401:
        print(f"Sarvam rejected the API key ({resp.text[:200]}).\n"
              "Use a Voice Agents workspace key: Voice Agents dashboard -> Settings -> API Key. Keys from the "
              "Sarvam API platform (indus.sarvam.ai, used by sarvam-mcp) don't work for calling.")
        sys.exit(1)
    if resp.status_code == 403:
        print(f"The key is valid but not allowed in this org/workspace ({resp.text[:200]}).\n"
              "Check SARVAM_ORG_ID / SARVAM_WORKSPACE_ID, and that the key was created in that workspace.")
        sys.exit(1)
    if resp.status_code >= 400:
        print(f"GET {url} -> {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


def _connections(config: dict) -> list[tuple[str, str]]:
    found = []
    for conn in (config or {}).get("connection_configs") or []:
        numbers = conn.get("phone_numbers") or [conn.get("agent_phone_number")]
        for num in numbers:
            num = num.get("phone_number") if isinstance(num, dict) else num
            if num:
                found.append((conn.get("connection_id", ""), str(num)))
    return found


def check() -> None:
    missing = [k for k, v in {"SARVAM_API_KEY": settings.sarvam_api_key, "SARVAM_ORG_ID": settings.sarvam_org_id,
                              "SARVAM_WORKSPACE_ID": settings.sarvam_workspace_id,
                              "SARVAM_APP_ID": settings.sarvam_app_id}.items() if not v]
    if missing:
        print("Fill these in .env first:", ", ".join(missing))
        sys.exit(1)
    org, ws, app = settings.sarvam_org_id, settings.sarvam_workspace_id, settings.sarvam_app_id
    sched = f"{ROOT}/api/scheduling/v1/orgs/{org}/workspaces/{ws}/campaigns"

    campaigns = (_get(sched, limit=50) or {}).get("items", [])
    print("API key, org and workspace: OK\n")

    mine = [c for c in campaigns if c.get("app_id") == app]
    pairs: dict[tuple[str, str], str] = {}
    for camp in mine[:5]:
        detail = _get(f"{sched}/{camp['campaign_id']}") or {}
        for pair in _connections(detail.get("agent_config") or {}):
            pairs.setdefault(pair, camp.get("name", ""))
    if pairs:
        print("Telephony used by this agent's campaigns:")
        for (conn, num), name in pairs.items():
            print(f"   SARVAM_CONNECTION_ID={conn}   SARVAM_AGENT_PHONE_NUMBER={num}   (campaign '{name}')")
    else:
        print(f"No campaigns found for {app}. Take SARVAM_CONNECTION_ID and SARVAM_AGENT_PHONE_NUMBER from the "
              "Phone Numbers page of the Voice Agents dashboard.")
    current = (settings.sarvam_connection_id, settings.sarvam_agent_phone_number)
    if all(current):
        print("   .env now:", "matches a campaign" if current in pairs else "not seen in campaigns (fine if it's new)")

    end = utcnow()
    data = _get(f"{settings.sarvam_analytics_base.rstrip('/')}/v1/{org}/{ws}/{app}/interactions",
                start_datetime=(end - timedelta(days=30)).isoformat(timespec="seconds"),
                end_datetime=end.isoformat(timespec="seconds"), limit=50, sort_by="start_datetime", sort_order="desc")
    items = (data or {}).get("items", [])
    print(f"\nCalls in the last 30 days: {(data or {}).get('total', 0)}")
    versions = sorted({i.get("app_version") for i in items if i.get("app_version") is not None})
    if versions:
        print(f"   agent versions used: {versions}   (.env SARVAM_APP_VERSION={settings.sarvam_app_version})")
    names = sorted({k for i in items for k in (i.get("agent_variables") or {})})
    if not names:
        print("   No agent variables seen yet. Place one test call, then run this again.")
        return
    print("   agent variable names:", ", ".join(names))
    inputs = [v for v in settings.agent_variable_map if v not in names]
    outputs = {col: key for col, key in settings.result_field_map.items() if key not in names}
    print("\nAGENT_VARIABLE_MAP (sheet -> agent):", "all found" if not inputs else f"not found: {', '.join(inputs)}")
    print("RESULT_FIELD_MAP (agent -> Excel):", "all found" if not outputs else
          "not found: " + ", ".join(f"{col} <- {key}" for col, key in outputs.items()))
    if inputs or outputs:
        print("Edit those maps in .env to use the names listed above.")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "check":
        check()
    else:
        print(__doc__)
