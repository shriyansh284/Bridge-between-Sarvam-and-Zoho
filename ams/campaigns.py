"""
Campaign = one spreadsheet attachment. Each row is one candidate call.

Row status flow
  scheduled -> calling -> completed                      (answered)
                       -> retry_scheduled -> calling ... (no answer / busy, until MAX_ATTEMPTS)
                       -> not_reachable                  (no answer after all attempts)
                       -> failed                         (DND, invalid number, provider error)
                       -> needs_review                   (no result arrived: never auto-redialed)
  invalid   (row had errors, never called)
  cancelled (stopped from the dashboard)
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .config import settings
from .email_settings import EMAIL_RE
from .sheet import RowCheck, SheetError, missing_columns, read_sheet, validate
from .store import (
    campaign_dir,
    campaign_tx,
    list_campaign_ids,
    load_campaign,
    new_campaign_id,
    save_campaign,
)
from .timeutil import fmt_local, iso, next_allowed_time, parse_iso, utcnow

log = logging.getLogger(__name__)

WAITING = {"scheduled", "retry_scheduled"}
ACTIVE = WAITING | {"calling"}
TERMINAL = {"invalid", "completed", "not_reachable", "failed", "cancelled", "needs_review"}
RESULT_STATUSES = {"connected", "no_answer", "busy", "failed"}
PLACED = RESULT_STATUSES | {"awaiting_result", "no_result"}

ROW_LABELS = {
    "scheduled": "Scheduled",
    "retry_scheduled": "Retry scheduled",
    "calling": "Calling",
    "completed": "Connected",
    "not_reachable": "No Answer",
    "failed": "Failed",
    "invalid": "Not called (invalid row)",
    "cancelled": "Cancelled",
    "needs_review": "Result pending (under review)",
}
ATTEMPT_LABELS = {
    "triggering": "Placing call", "awaiting_result": "Waiting for result", "connected": "Connected",
    "no_answer": "No Answer", "busy": "Busy", "failed": "Failed", "trigger_error": "Not placed (API error)",
    "no_result": "No result received",
}
STATUS_MAP = {"connected": "connected", "completed": "connected", "answered": "connected",
              "no_answer": "no_answer", "not_answered": "no_answer", "busy": "busy", "failed": "failed"}


# ---------------------------------------------------------------- create
def _row_from_check(chk: RowCheck, index: int) -> dict:
    d = chk.data
    agent_vars = {var: d.get(fld, "") for var, fld in settings.agent_variable_map.items()}
    overrides = {k: d[k] for k in ("initial_bot_message", "initial_state_name", "initial_language_name") if d.get(k)}
    return {
        "row_id": f"R{index:04d}",
        "excel_row": chk.excel_row,
        "candidate_id": d.get("candidate_id", ""),
        "name": d.get("candidate_name", ""),
        "phone": d.get("phone_e164") or d.get("phone_number", ""),
        "company": d.get("hiring_company", ""),
        "documents": d.get("pending_documents", ""),
        "slot": chk.slot,
        "scheduled_at": iso(chk.scheduled_utc),
        "next_call_at": iso(chk.effective_utc),
        "status": "scheduled" if chk.ok else "invalid",
        "errors": chk.errors,
        "warnings": chk.warnings,
        "note": "; ".join(chk.errors or chk.warnings),
        "agent_variables": agent_vars,
        "app_overrides": overrides or None,
        "attempt_count": 0,
        "trigger_errors": 0,
        "attempts": [],
        "outcome": None,
    }


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_campaign(message_id: str, attachment_id: str) -> Optional[str]:
    for cid in list_campaign_ids():
        camp = load_campaign(cid) or {}
        src = camp.get("email") or {}
        if src.get("message_id") == message_id and src.get("attachment_id") == attachment_id:
            return cid
    return None


def find_duplicate_file(sha: str, sender: str) -> Optional[str]:
    cutoff = utcnow() - timedelta(days=settings.duplicate_file_days)
    for cid in list_campaign_ids():
        camp = load_campaign(cid) or {}
        original = camp["original_sender"] if "original_sender" in camp else camp.get("reply_to")
        if (camp.get("file_sha256") == sha and (original or "").lower() == (sender or "").lower()
                and camp.get("status") != "rejected" and parse_iso(camp["created_at"]) >= cutoff):
            return cid
    return None


def create_campaign(data: bytes, filename: str, source: str, reply_to: Optional[str] = None,
                    email: Optional[dict] = None, created_by: str = "") -> dict:
    """Validate the file and create the campaign. Raises SheetError when the file is unusable."""
    sheet = read_sheet(data, filename)
    missing = missing_columns(sheet)
    if missing:
        raise SheetError("Missing columns: " + ", ".join(missing))
    if not sheet.rows:
        raise SheetError("The sheet has a header but no candidate rows.")

    checks = validate(sheet)
    cid = new_campaign_id()
    ext = Path(filename).suffix.lower() or ".xlsx"
    (campaign_dir(cid) / f"input{ext}").write_bytes(data)
    rows = [_row_from_check(chk, i + 1) for i, chk in enumerate(checks)]
    valid = [r for r in rows if r["status"] != "invalid"]
    camp = {
        "campaign_id": cid,
        "created_at": iso(utcnow()),
        "source": source,
        "created_by": created_by,
        "email": email,
        "reply_to": reply_to,
        "file_name": filename,
        "input_file": f"input{ext}",
        "file_sha256": file_sha(data),
        "file_kind": sheet.kind,
        "sheet_name": sheet.sheet_name,
        "header_row": sheet.header_row,
        "headers": sheet.headers,
        "status": "scheduled" if valid else "rejected",
        "rows": rows,
        "emails": [],
        "slots_sent": [],
        "last_update_done": 0,
        "final_sent": False,
    }
    if reply_to and settings.send_ack:
        queue_email(camp, "rejected" if not valid else "ack", "ack")
    save_campaign(camp)
    log.info("Created %s from %s: %s rows, %s valid", cid, filename, len(rows), len(valid))
    return camp


# ---------------------------------------------------------------- helpers
def row_by_id(camp: dict, row_id: str) -> dict:
    return next(r for r in camp["rows"] if r["row_id"] == row_id)


def counts(camp: dict) -> dict:
    c = {"total": len(camp["rows"]), "valid": 0, "done": 0, "connected": 0, "no_answer": 0, "failed": 0,
         "invalid": 0, "cancelled": 0, "review": 0, "waiting": 0, "calling": 0}
    for r in camp["rows"]:
        s = r["status"]
        if s != "invalid":
            c["valid"] += 1
        if s in TERMINAL and s != "invalid":
            c["done"] += 1
        c["connected"] += s == "completed"
        c["no_answer"] += s == "not_reachable"
        c["failed"] += s == "failed"
        c["invalid"] += s == "invalid"
        c["cancelled"] += s == "cancelled"
        c["review"] += s == "needs_review"
        c["waiting"] += s in WAITING
        c["calling"] += s == "calling"
    return c


def refresh_status(camp: dict) -> None:
    if camp["status"] == "rejected":
        return
    c = counts(camp)
    if c["valid"] and c["done"] == c["valid"]:
        camp["status"] = "completed"
    elif c["calling"] or any(r["attempts"] for r in camp["rows"]):
        camp["status"] = "in_progress"
    else:
        camp["status"] = "scheduled"


def next_call_time(camp: dict) -> Optional[str]:
    times = [r["next_call_at"] for r in camp["rows"] if r["status"] in WAITING and r.get("next_call_at")]
    return min(times) if times else None


def _placed(row: dict) -> int:
    return sum(1 for a in row["attempts"] if a["status"] in PLACED)


# ---------------------------------------------------------------- calling
def due_row_ids(camp: dict, now) -> list[str]:
    due = [r for r in camp["rows"] if r["status"] in WAITING and r.get("next_call_at")
           and parse_iso(r["next_call_at"]) <= now]
    due.sort(key=lambda r: r["next_call_at"])
    return [r["row_id"] for r in due]


def claim_row(cid: str, row_id: str) -> Optional[dict]:
    """Mark the row as calling and write the attempt BEFORE dialing. None if someone else got it."""
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        if row["status"] not in WAITING:
            return None
        attempt = {"ref": uuid.uuid4().hex, "number": len(row["attempts"]) + 1, "status": "triggering",
                   "triggered_at": iso(utcnow()), "sarvam_attempt_id": None}
        row["attempts"].append(attempt)
        row["status"] = "calling"
        refresh_status(camp)
        return {"attempt": attempt, "phone": row["phone"], "agent_variables": row["agent_variables"],
                "app_overrides": row["app_overrides"]}


def trigger_succeeded(cid: str, row_id: str, ref: str, sarvam_id: str) -> None:
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        att = next(a for a in row["attempts"] if a["ref"] == ref)
        att["sarvam_attempt_id"] = sarvam_id
        if att["status"] == "triggering":  # the result webhook may already have arrived
            att["status"] = "awaiting_result"
        row["attempt_count"] = _placed(row)
        row["trigger_errors"] = 0
        row["last_called_at"] = att["triggered_at"]


def trigger_failed(cid: str, row_id: str, ref: str, message: str, retryable: bool) -> None:
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        att = next(a for a in row["attempts"] if a["ref"] == ref)
        att["status"] = "trigger_error"
        att["failure_reason"] = message
        row["trigger_errors"] += 1
        row["note"] = message
        if retryable and row["trigger_errors"] < settings.max_trigger_errors:
            row["status"] = "retry_scheduled"
            row["next_call_at"] = iso(utcnow() + timedelta(minutes=settings.trigger_error_backoff_minutes))
        elif retryable:
            row["status"] = "needs_review"
        else:
            row["status"] = "failed"
        refresh_status(camp)


def trigger_blocked(cid: str, row_id: str, ref: str, message: str) -> None:
    """The call wasn't placed because of an account problem (API key / IDs rejected), not because of
    this candidate. Undo the claim: the row waits and is called once the problem is fixed."""
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        row["attempts"] = [a for a in row["attempts"] if a["ref"] != ref]
        row["status"] = "retry_scheduled" if row["attempts"] else "scheduled"
        row["note"] = f"Not called yet: {message}"
        refresh_status(camp)


def flag_stale(camp: dict, now) -> int:
    """Inside a campaign_tx: attempts with no result in time -> needs_review."""
    flagged = 0
    for row in camp["rows"]:
        for att in row["attempts"]:
            started = parse_iso(att["triggered_at"])
            late = (att["status"] == "awaiting_result" and started < now - timedelta(minutes=settings.result_timeout_minutes)) \
                or (att["status"] == "triggering" and started < now - timedelta(minutes=10))
            if late:
                att["status"] = "no_result"
                att["failure_reason"] = "No result received from Sarvam in time"
                if row["status"] == "calling" and att is row["attempts"][-1]:
                    row["status"] = "needs_review"
                    row["note"] = "No call result received. Check the Sarvam dashboard before retrying."
                row["attempt_count"] = _placed(row)
                flagged += 1
    if flagged:
        refresh_status(camp)
    return flagged


# ---------------------------------------------------------------- results
LATE_RESULT_HOURS = 24  # calls marked "under review" are still looked up this long, so a late result can rescue them


def awaiting_attempts(now) -> list[dict]:
    """Placed calls whose result hasn't arrived yet, in campaigns that are still open."""
    out = []
    for cid in list_campaign_ids():
        if (campaign_dir(cid) / ".done").exists():
            continue
        camp = load_campaign(cid) or {}
        if camp.get("status") == "rejected":
            continue
        for row in camp.get("rows", []):
            for att in row["attempts"]:
                if not att.get("sarvam_attempt_id"):
                    continue
                late = att["status"] == "no_result" and parse_iso(att["triggered_at"]) > now - timedelta(hours=LATE_RESULT_HOURS)
                if att["status"] == "awaiting_result" or late:
                    out.append({"campaign_id": cid, "row_id": row["row_id"], "ref": att["ref"],
                                "sarvam_attempt_id": att["sarvam_attempt_id"], "triggered_at": att["triggered_at"]})
    return out


def normalize_payload(payload: dict) -> dict:
    raw = str(payload.get("status") or payload.get("connectivity_status") or "").lower()
    meta = (payload.get("webhook_config") or {}).get("metadata") or {}
    return {
        "attempt_id": payload.get("attempt_id"), "status": STATUS_MAP.get(raw), "raw_status": raw,
        "duration": payload.get("duration"), "interaction_id": payload.get("interaction_id"),
        "app_id": payload.get("app_id"), "failure_reason": payload.get("failure_reason"),
        "variables": payload.get("final_agent_variables") or payload.get("output_agent_variables"),
        "transcript": payload.get("interaction_transcript"), "recording_url": payload.get("recording_url"),
        "audio_url": payload.get("audio_url"),  # Sarvam's own (signed) recording link, from fetched results
        "campaign_id": meta.get("campaign_id"), "row_id": meta.get("row_id"), "ref": meta.get("ref"),
    }


def _locate(n: dict) -> Optional[tuple[str, str, str]]:
    if n["campaign_id"] and n["row_id"] and n["ref"] and load_campaign(n["campaign_id"]):
        return n["campaign_id"], n["row_id"], n["ref"]
    if n["attempt_id"]:  # fallback: search active campaigns by Sarvam attempt id
        for cid in list_campaign_ids():
            camp = load_campaign(cid) or {}
            for row in camp.get("rows", []):
                for att in row["attempts"]:
                    if att.get("sarvam_attempt_id") == str(n["attempt_id"]):
                        return cid, row["row_id"], att["ref"]
    return None


def apply_result(payload: dict) -> str:
    n = normalize_payload(payload)
    where = _locate(n)
    if not where:
        return "unmatched"
    cid, row_id, ref = where
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        att = next((a for a in row["attempts"] if a["ref"] == ref), None)
        if att is None:
            return "unmatched"
        if att["status"] in RESULT_STATUSES and att.get("result_at"):
            return "duplicate"
        now = utcnow()
        att.update({
            "status": n["status"] or "failed", "result_at": iso(now), "duration": n["duration"],
            "interaction_id": n["interaction_id"], "app_id": n["app_id"], "variables": n["variables"],
            "transcript": n["transcript"], "recording_url": n["recording_url"], "audio_url": n["audio_url"],
            "failure_reason": n["failure_reason"] or (None if n["status"] else f"Unknown status '{n['raw_status']}'"),
        })
        if n["attempt_id"] and not att.get("sarvam_attempt_id"):
            att["sarvam_attempt_id"] = str(n["attempt_id"])
        row["attempt_count"] = _placed(row)
        if att is not row["attempts"][-1] or row["status"] == "cancelled":
            return "processed_old_attempt"

        if att["status"] == "connected":
            row["status"] = "completed"
            row["outcome"] = att["variables"]
            row["note"] = ""
        elif att["status"] in ("no_answer", "busy"):
            if row["attempt_count"] < settings.max_attempts:
                row["status"] = "retry_scheduled"
                row["next_call_at"] = iso(next_allowed_time(now + timedelta(minutes=settings.retry_gap_minutes)))
                row["note"] = f"{ATTEMPT_LABELS[att['status']]}; retrying at {fmt_local(row['next_call_at'])}"
            else:
                row["status"] = "not_reachable"
                row["note"] = f"{ATTEMPT_LABELS[att['status']]} after {row['attempt_count']} attempt(s)"
        else:
            row["status"] = "failed"
            row["note"] = att["failure_reason"] or "Call could not be placed"
        refresh_status(camp)
    return "processed"


# ---------------------------------------------------------------- result emails
def queue_email(camp: dict, kind: str, key: str, info: Optional[dict] = None) -> dict:
    rec = {"key": key, "kind": kind, "status": "pending", "failures": 0, "created_at": iso(utcnow()),
           "counts": counts(camp), **(info or {})}
    camp["emails"].append(rec)
    return rec


def decide_emails(camp: dict) -> None:
    """Inside a campaign_tx: queue the next update / final email if one is due."""
    if not camp.get("reply_to") or camp["status"] == "rejected" or camp.get("final_sent"):
        return
    if any(e["kind"] == "final" and e["status"] in ("pending", "sending", "failed") for e in camp["emails"]):
        return  # a final email is already on its way (or needs a manual resend)
    valid = [r for r in camp["rows"] if r["status"] != "invalid"]
    if not valid:
        return
    if all(r["status"] in TERMINAL for r in valid):
        n = sum(1 for e in camp["emails"] if e["kind"] == "final") + 1
        queue_email(camp, "final", f"final-{n}", {"slots": sorted({r["slot"] for r in valid})})
        return
    if settings.batch_mode == "slot":
        slots: dict[str, list] = {}
        for r in valid:
            slots.setdefault(r["slot"], []).append(r)
        finished = [s for s, rows in slots.items()
                    if s not in camp["slots_sent"] and all(r["status"] in TERMINAL for r in rows)]
        if finished:
            camp["slots_sent"].extend(finished)
            n = sum(1 for e in camp["emails"] if e["kind"] == "update") + 1
            queue_email(camp, "update", f"update-{n}", {"slots": sorted(finished), "update_no": n})
    elif settings.batch_mode == "size":
        done = counts(camp)["done"]
        if done - camp.get("last_update_done", 0) >= settings.batch_size:
            camp["last_update_done"] = done
            n = sum(1 for e in camp["emails"] if e["kind"] == "update") + 1
            queue_email(camp, "update", f"update-{n}", {"update_no": n})


def slot_label(slot: str) -> str:
    if slot == "Immediate" or not slot:
        return "sent for immediate calling"
    try:
        from datetime import datetime

        return datetime.strptime(slot, "%Y-%m-%d %H:%M").strftime("%d %b %Y, %I:%M %p")
    except ValueError:
        return slot


# ---------------------------------------------------------------- dashboard actions
def reopen(camp: dict) -> None:
    """Inside a campaign_tx: a finished campaign gets more calls, so a new final file will follow."""
    camp["final_sent"] = False
    (campaign_dir(camp["campaign_id"]) / ".done").unlink(missing_ok=True)


def row_action(cid: str, row_id: str, action: str) -> str:
    now = utcnow()
    with campaign_tx(cid) as camp:
        row = row_by_id(camp, row_id)
        if action == "call_now" and row["status"] in WAITING:
            row["next_call_at"] = iso(next_allowed_time(now))
            row["note"] = "Moved to now from the dashboard"
            msg = "Queued. The call goes out within a minute (inside calling hours)."
        elif action == "cancel" and row["status"] in WAITING:
            row["status"] = "cancelled"
            row["note"] = "Cancelled from the dashboard"
            msg = "Call cancelled."
        elif action == "call_again" and row["status"] in TERMINAL - {"invalid"}:
            row.update(status="scheduled", next_call_at=iso(next_allowed_time(now)), trigger_errors=0,
                       note="Call again requested from the dashboard")
            reopen(camp)
            msg = "One more call is queued. A new final file will be sent when it finishes."
        else:
            return "That action isn't available for this row any more. Refresh and try again."
        refresh_status(camp)
    return msg


def change_recipient(cid: str, address: str, user: str = "") -> str:
    """Send this campaign's remaining emails (updates, final, resends) to another address,
    e.g. when the client's email id is replaced. Replies stay in the original email thread."""
    address = (address or "").strip().lower()
    if not EMAIL_RE.match(address):
        return f"'{address}' isn't a valid email address."
    with campaign_tx(cid) as camp:
        old = camp.get("reply_to")
        if old == address:
            return f"Results for {cid} already go to {address}."
        camp.setdefault("original_sender", old)
        camp["reply_to"] = address
        camp.setdefault("recipient_changes", []).append({"at": iso(utcnow()), "by": user, "from": old, "to": address})
        (campaign_dir(cid) / ".done").unlink(missing_ok=True)  # a finished campaign without an address gets its final
        already_final = bool(camp.get("final_sent"))
    msg = f"Results for {cid} will now be emailed to {address}" + (f" instead of {old}." if old else ".")
    if already_final:
        msg += " The final file was already sent; use Resend last email to send it to the new address."
    return msg


def campaign_action(cid: str, action: str) -> str:
    with campaign_tx(cid) as camp:
        if action == "cancel_remaining":
            n = 0
            for row in camp["rows"]:
                if row["status"] in WAITING:
                    row["status"], row["note"] = "cancelled", "Cancelled from the dashboard"
                    n += 1
            refresh_status(camp)
            return f"Cancelled {n} waiting call(s). Calls already in progress will still report back."
        if action == "send_update":
            if not camp.get("reply_to"):
                return "This campaign has no email address to send to. Download the file instead."
            n = sum(1 for e in camp["emails"] if e["kind"] == "update") + 1
            queue_email(camp, "update", f"update-{n}", {"update_no": n, "manual": True})
            (campaign_dir(cid) / ".done").unlink(missing_ok=True)
            return "An update with the current results will be emailed within a minute."
        if action == "resend_last":
            if not camp["emails"]:
                return "No email has been sent for this campaign yet."
            last = camp["emails"][-1]
            last.update(status="pending", failures=0, error=None)
            (campaign_dir(cid) / ".done").unlink(missing_ok=True)
            return f"The last email ({last['kind']}) will be sent again within a minute."
    return "Unknown action."
