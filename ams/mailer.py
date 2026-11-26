"""
Email in, email out.

poll_inbox():   unread emails with attachments in the fixed Zoho address ->
                each spreadsheet attachment becomes one campaign.
send_due_emails(): sends queued ack / update / final / rejection replies in
                the client's original thread, with the result Excel attached.
                For a file that came through the SFTP folder (ams/dropfolder.py)
                the same results are written there as files instead.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from filelock import Timeout

from . import email_settings
from .campaigns import counts, create_campaign, decide_emails, delivers, file_sha, find_campaign, \
    find_duplicate_file, next_call_time, slot_label
from .config import BASE_DIR, settings
from .dropfolder import deliver as deliver_to_drop
from .excel_out import build_results, output_name
from .sheet import CLIENT_FORMAT, SheetError
from .store import (STATE, append_log, campaign_dir, campaign_tx, get_status, list_campaign_ids, load_campaign, lock,
                    read_json, set_status, write_json)
from .timeutil import fmt_local, iso, parse_iso, stamp, utcnow
from .zoho import ZohoError, client, extract_email, sender_allowed

log = logging.getLogger(__name__)
SPREADSHEET_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
TEMPLATE = BASE_DIR / "templates" / "AMS_Calling_Template.csv"  # the predefined client format
PROCESSED = STATE / "processed_messages.json"


# ================================================================ inbound
def _mark_processed(message_id: str, info: dict) -> None:
    with lock("processed"):
        data = read_json(PROCESSED, {})
        data[message_id] = {"at": iso(utcnow()), **info}
        write_json(PROCESSED, data)


def _already_processed(message_id: str) -> bool:
    return message_id in read_json(PROCESSED, {})


def held_emails() -> list[dict]:
    """Emails skipped because the sender wasn't allowed. They stay unread in the inbox."""
    return [{"message_id": mid, **info} for mid, info in read_json(PROCESSED, {}).items()
            if info.get("result") == "ignored_sender"]


def release_held_emails() -> int:
    """After the allowed senders change: forget the skips for senders that are now allowed,
    so the next inbox check processes those emails (they must still be unread in Zoho)."""
    with lock("processed"):
        data = read_json(PROCESSED, {})
        held = [mid for mid, info in data.items()
                if info.get("result") == "ignored_sender" and sender_allowed(info.get("from", ""))]
        for mid in held:
            data.pop(mid)
        if held:
            write_json(PROCESSED, data)
    for mid in held:
        append_log("mail_log", {"message_id": mid, "result": "released", "reason": "Sender now allowed"})
    return len(held)


def poll_inbox() -> dict:
    """One pass over the inbox. Safe to call from several processes (only one runs at a time)."""
    stats = {"emails": 0, "campaigns": 0, "ignored": 0, "rejected": 0}
    if not settings.mail_enabled or settings.missing_for_mail():
        return stats
    try:
        with lock("mail-poll", timeout=0):
            zm = client()
            messages = zm.unread_with_attachments()
            for msg in messages:
                mid = str(msg["messageId"])
                if _already_processed(mid):
                    continue
                stats["emails"] += 1
                result = _handle_message(zm, msg)
                stats[result] = stats.get(result, 0) + 1
            set_status("mail_last_check", {"at": iso(utcnow()), "ok": True, "seen": len(messages)})
    except Timeout:
        pass  # another process is polling right now
    except ZohoError as exc:
        log.error("Mail poll failed: %s", exc)
        set_status("mail_last_check", {"at": iso(utcnow()), "ok": False, "error": str(exc)[:500]})
    return stats


def mail_cutoff() -> Optional[datetime]:
    """Emails received before the backend was first started are never processed."""
    value = get_status().get("mail_cutoff")
    return parse_iso(value) if value else None


def ensure_mail_cutoff() -> None:
    """Called when the backend starts. The first time ever, records 'now' as go-live, so the
    mail already sitting in the inbox (old calling sheets etc.) can never start calls."""
    if not get_status().get("mail_cutoff"):
        set_status("mail_cutoff", iso(utcnow()))
        log.info("Go-live recorded: only emails received from now on start campaigns")


def _received(msg: dict) -> Optional[datetime]:
    try:  # naive UTC, like every other time in this project (timeutil.utcnow)
        return datetime.fromtimestamp(int(msg.get("receivedTime")) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _handle_message(zm, msg: dict) -> str:
    mid = str(msg["messageId"])
    folder = str(msg.get("folderId") or zm.folder_id())
    sender = extract_email(msg.get("fromAddress") or msg.get("sender") or "")
    subject = html.unescape(msg.get("subject") or "(no subject)")
    meta = {"message_id": mid, "folder_id": folder, "thread_id": msg.get("threadId"), "from": sender,
            "subject": subject, "received_at": msg.get("receivedTime")}

    cutoff, received = mail_cutoff(), _received(msg)
    if cutoff and received and received < cutoff:
        # old mail already sitting in the inbox at go-live: never start calls from it (forward it again if needed)
        append_log("mail_log", {**meta, "result": "skipped", "reason": "Received before the system went live"})
        _mark_processed(mid, {"result": "before_go_live", "from": sender, "subject": subject})
        return "old"

    if not sender_allowed(sender):
        append_log("mail_log", {**meta, "result": "ignored",
                                "reason": "Sender not allowed (add it in the dashboard's Email settings)"})
        _mark_processed(mid, {"result": "ignored_sender", "from": sender, "subject": subject,
                              "received_at": meta["received_at"]})
        return "ignored"  # left unread; processed automatically if the sender is allowed later

    sheets = [a for a in zm.attachments(folder, mid)
              if str(a.get("attachmentName", "")).lower().endswith(SPREADSHEET_EXT)]
    if not sheets:
        append_log("mail_log", {**meta, "result": "ignored", "reason": "No .xlsx/.csv attachment"})
        _mark_processed(mid, {"result": "no_spreadsheet", "from": sender})
        return "ignored"

    created = []
    for att in sheets:
        att_id, name = str(att["attachmentId"]), att["attachmentName"]
        existing = find_campaign(mid, att_id)
        if existing:  # crash-safe: never create the same campaign twice
            created.append(existing)
            continue
        data = zm.download(folder, mid, att_id)
        email = {**meta, "attachment_id": att_id, "attachment_name": name}
        created.append(handle_file(data, name, sender, email))
    _mark_processed(mid, {"result": "campaigns", "campaigns": [c for c in created if c], "from": sender})
    try:
        zm.mark_read([mid])
    except ZohoError as exc:
        log.warning("Could not mark %s as read: %s", mid, exc)
    return "campaigns" if any(created) else "rejected"


def handle_file(data: bytes, filename: str, sender: Optional[str], email: Optional[dict],
                source: str = "email", created_by: str = "") -> Optional[str]:
    """Create a campaign from one file (email attachment or dashboard upload). Returns campaign id or None."""
    base_log = {"file": filename, "from": sender, "message_id": (email or {}).get("message_id")}
    dup = find_duplicate_file(file_sha(data), sender) if sender else None
    if dup:
        append_log("mail_log", {**base_log, "result": "duplicate", "campaign": dup})
        _send_plain(email, sender, f"Already received: {filename}",
                    _p(f"This file was already received as campaign <b>{dup}</b> and is being processed. "
                       "No calls were placed again. Results for that campaign will arrive in its original thread."))
        return None
    try:
        camp = create_campaign(data, filename, source=source, reply_to=sender, email=email, created_by=created_by)
    except SheetError as exc:
        append_log("mail_log", {**base_log, "result": "rejected", "reason": str(exc)})
        attach = [(TEMPLATE.name, TEMPLATE.read_bytes())] if TEMPLATE.exists() else []
        _send_plain(email, sender, f"Could not process {filename}",
                    _p(f"We couldn't process <b>{html.escape(filename)}</b>: {html.escape(str(exc))}")
                    + _p("No calls were placed. Please fill the attached template, one candidate per row, "
                         "and send it again.") + format_table(), attach)
        return None
    append_log("mail_log", {**base_log, "result": "campaign", "campaign": camp["campaign_id"],
                            "rows": len(camp["rows"]), "status": camp["status"]})
    return camp["campaign_id"]


# ================================================================ outbound
def _p(text: str) -> str:
    return f"<p style='font-family:Arial,sans-serif;font-size:14px;color:#222'>{text}</p>"


def _table(pairs: list[tuple[str, object]]) -> str:
    rows = "".join(f"<tr><td style='padding:4px 14px 4px 0;color:#555'>{html.escape(str(k))}</td>"
                   f"<td style='padding:4px 0'><b>{html.escape(str(v))}</b></td></tr>" for k, v in pairs)
    return f"<table style='font-family:Arial,sans-serif;font-size:14px;border-collapse:collapse'>{rows}</table>"


def format_table() -> str:
    """The predefined file format, for emails that ask the client to fix their file."""
    cell = "padding:4px 10px;border:1px solid #ddd;vertical-align:top"
    rows = "".join(f"<tr><td style='{cell}'><b>{html.escape(c)}</b></td><td style='{cell}'>{html.escape(r)}</td>"
                   f"<td style='{cell}'>{html.escape(t)}</td><td style='{cell}'>{html.escape(e)}</td></tr>"
                   for c, r, t, e in CLIENT_FORMAT)
    head = "".join(f"<th style='{cell};background:#f2f2f2;text-align:left'>{h}</th>"
                   for h in ("Column", "Required", "What to write", "Example"))
    return (_p("The file must have these columns, in this order:")
            + f"<table style='font-family:Arial,sans-serif;font-size:13px;border-collapse:collapse'>"
              f"<tr>{head}</tr>{rows}</table>")


def _signature() -> str:
    return _p("Regards,<br>AMS Calling Team")


def _reply_subject(camp_or_email: Optional[dict], fallback: str) -> str:
    subject = (camp_or_email or {}).get("subject") or fallback
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _deliver(email: Optional[dict], to: str, subject: str, body: str,
             files: Optional[list[tuple[str, bytes]]] = None) -> None:
    zm = client()
    cc = email_settings.cc()
    uploaded = [zm.upload(name, data) for name, data in (files or [])]
    if email and email.get("message_id"):
        zm.reply(email["message_id"], to, _reply_subject(email, subject), body, uploaded, cc)
    else:
        zm.send(to, subject, body, uploaded, cc)


def _send_plain(email, to, subject, body, files=None) -> None:
    if not to or not settings.mail_enabled or settings.missing_for_mail():
        return
    try:
        _deliver(email, to, subject, body + _signature(), files)
    except ZohoError as exc:
        log.error("Email to %s failed: %s", to, exc)
        append_log("mail_log", {"result": "send_failed", "to": to, "subject": subject, "error": str(exc)[:300]})


def compose(camp: dict, rec: dict) -> tuple[str, str, Optional[tuple[str, bytes]]]:
    """(subject, html body, attachment) for one queued email."""
    c = counts(camp)
    cid, fname = camp["campaign_id"], html.escape(camp["file_name"])
    kind = rec["kind"]
    if kind == "ack":
        waiting = [r for r in camp["rows"] if r["status"] != "invalid"]
        first = min((r["next_call_at"] for r in waiting), default=None)
        last = max((r["next_call_at"] for r in waiting), default=None)
        body = _p(f"We've received <b>{fname}</b> and created campaign <b>{cid}</b>.") + _table([
            ("Candidates in file", c["total"]), ("Scheduled for calling", c["valid"]),
            ("First call", fmt_local(first)), ("Last call", fmt_local(last)), ("Rows that can't be called", c["invalid"]),
        ])
        bad = [r for r in camp["rows"] if r["status"] == "invalid"]
        if bad:
            items = "".join(f"<li>Row {r['excel_row']} ({html.escape(r['name'] or r['candidate_id'])}): "
                            f"{html.escape('; '.join(r['errors']))}</li>" for r in bad[:50])
            body += _p("These rows won't be called:") + f"<ul style='font-family:Arial;font-size:14px'>{items}</ul>"
        body += _p("You'll receive an updated result file as each scheduled call time finishes, "
                   "and a final file once every call in this campaign is complete.")
        return f"Received: {camp['file_name']} ({cid})", body, None

    if kind == "rejected":
        items = "".join(f"<li>Row {r['excel_row']}: {html.escape('; '.join(r['errors']))}</li>" for r in camp["rows"][:50])
        body = _p(f"We couldn't schedule any calls from <b>{fname}</b>. No calls were placed.") + \
            f"<ul style='font-family:Arial;font-size:14px'>{items}</ul>" + \
            _p("The attached file shows the problem next to each row. Please correct it and send it again.") + \
            format_table()
        return f"Could not process {camp['file_name']}", body, (output_name(camp, "check"), build_results(camp))

    final = kind == "final"
    summary = _table([
        ("Campaign", cid), ("Calls finished", f"{c['done']} of {c['valid']}"), ("Connected", c["connected"]),
        ("No answer / busy", c["no_answer"]), ("Failed", c["failed"]),
        *([("Result pending (under review)", c["review"])] if c["review"] else []),
        *([("Cancelled", c["cancelled"])] if c["cancelled"] else []),
        *([("Not called (invalid rows)", c["invalid"])] if c["invalid"] else []),
        *([] if final else [("Still scheduled", c["waiting"] + c["calling"]),
                            ("Next call", fmt_local(next_call_time(camp)) or "None")]),
    ])
    if final:
        intro = _p(f"All calls for <b>{fname}</b> are finished. The attached file is the final result for "
                   f"campaign <b>{cid}</b>.")
    else:
        slots = ", ".join(slot_label(s) for s in rec.get("slots", [])) or "the latest batch"
        intro = _p(f"Update {rec.get('update_no', '')}: calls scheduled for <b>{html.escape(slots)}</b> are finished. "
                   "The attached file includes every result available so far; more updates will follow.")
    outro = _p("Each row shows the call status, time, duration, the candidate's response and a link to the "
               "recording where the call connected.")
    attach = (output_name(camp, kind, rec.get("update_no")), build_results(camp, final=final))
    subject = f"{'Final results' if final else 'Calling update'}: {camp['file_name']} ({cid})"
    return subject, intro + summary + outro, attach


STUCK_SENDING_MINUTES = 10  # an email still "sending" after this long was cut off (restart, crash): retry it


def send_due_emails() -> int:
    """Queue any due update/final emails, then send everything pending. Returns emails sent."""
    sent = 0
    for cid in list_campaign_ids():
        if (campaign_dir(cid) / ".done").exists():
            continue
        with campaign_tx(cid) as camp:
            stuck_before = utcnow() - timedelta(minutes=STUCK_SENDING_MINUTES)
            for e in camp["emails"]:
                if e["status"] == "sending" and parse_iso(e.get("sending_at") or e["created_at"]) < stuck_before:
                    e.update(status="failed", failures=e["failures"] + 1,
                             error="Interrupted while sending (restart?); it may already have been delivered")
            decide_emails(camp)
            pending = [i for i, e in enumerate(camp["emails"])
                       if e["status"] in ("pending", "failed") and e["failures"] < settings.max_email_failures]
            for i in pending:
                camp["emails"][i]["status"] = "sending"
                camp["emails"][i]["sending_at"] = stamp()
        for i in pending:
            sent += _send_one(cid, i)
        with campaign_tx(cid) as camp:  # under the lock, so "Call again" can't slip in between check and marker
            if _campaign_closed(camp):
                (campaign_dir(cid) / ".done").touch()
    return sent


def _campaign_closed(camp: dict) -> bool:
    if any(e["status"] in ("pending", "sending", "failed") and e["failures"] < settings.max_email_failures
           for e in camp["emails"]):
        return False
    if camp["status"] == "rejected":
        return True
    return camp["status"] == "completed" and (camp.get("final_sent") or not delivers(camp))


def _send_one(cid: str, index: int) -> int:
    """Send one queued result: by email, or for a file that came through the SFTP folder, as a file there."""
    camp = load_campaign(cid)
    rec = camp["emails"][index]
    ok, error, saved, saved_only = False, None, None, False
    try:
        if camp.get("drop"):
            saved = deliver_to_drop(camp, rec)
            ok = True
        else:
            saved, ok, saved_only, error = _email_one(camp, rec)
    except Exception as exc:  # keep the loop alive; retried next cycle
        log.exception("Writing the %s file for %s to the SFTP folder failed", rec["kind"], cid)
        error = str(exc)[:500]
    with campaign_tx(cid) as camp:
        rec = camp["emails"][index]
        rec["file"] = saved or rec.get("file")
        if camp.get("drop"):
            rec["via"] = "sftp"
        if ok or saved_only:
            rec.update(status="sent" if ok else "saved", sent_at=iso(utcnow()), error=None)
            # A final composed before the campaign was reopened ("Call again", a late result) is out of date:
            # it doesn't finish the campaign, so the corrected final still follows.
            if rec["kind"] == "final" and (rec.get("sending_at") or "") > (camp.get("reopened_at") or ""):
                camp["final_sent"] = True
        else:
            rec.update(status="failed", failures=rec["failures"] + 1, error=error)
    return int(ok)


def _email_one(camp: dict, rec: dict) -> tuple[Optional[str], bool, bool, Optional[str]]:
    """(saved file, sent, saved only, error) for one queued email."""
    cid = camp["campaign_id"]
    ok, error, saved, saved_only = False, None, None, False
    try:
        subject, body, attach = compose(camp, rec)
        if attach:
            out_dir = campaign_dir(cid) / "outputs"
            out_dir.mkdir(exist_ok=True)
            (out_dir / attach[0]).write_bytes(attach[1])
            saved = f"outputs/{attach[0]}"
        if not settings.mail_enabled:
            saved_only = True  # MAIL_ENABLED=false: keep the file for download, don't send
        elif settings.missing_for_mail():
            error = "Zoho settings missing: " + ", ".join(settings.missing_for_mail())
        else:
            _deliver(camp.get("email"), camp["reply_to"], subject, body + _signature(), [attach] if attach else [])
            ok = True
    except Exception as exc:  # keep the loop alive; retried next cycle
        log.exception("Sending %s email for %s failed", rec["kind"], cid)
        error = str(exc)[:500]
    return saved, ok, saved_only, error
