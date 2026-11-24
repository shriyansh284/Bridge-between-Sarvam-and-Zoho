"""
Call results: fetched from Sarvam, so this server needs NO public address.

Every RESULT_POLL_SECONDS the scheduler calls poll_results(): it asks Sarvam's Analytics API
about the calls still waiting for a result (by the attempt_id Sarvam gave us when the call was
placed), and applies each finished one exactly like a webhook would. If PUBLIC_BASE_URL is set,
Sarvam's webhook also delivers results straight away; whichever arrives first wins and the other
is ignored as a duplicate.

Analytics record -> result
  connectivity_status  connected | no_answer | busy | failed   (anything else = still in progress)
  connected calls count as finished once they have an end_datetime; for up to GRACE_MINUTES after
  hang-up we wait for the agent's outputs and the transcript to be saved before taking the result
  failure_reason       NO_FAILURE_REASON means none
  agent_variables      the agent's outputs (used for connected calls)
  audio_url            Sarvam's signed recording link (opens without a login when the workspace
                       has "Public signed URLs" on; expires after 14 days)
  transcript           fetched separately for connected calls

Safety rules
  * A call is only given up on ("under review") after a SUCCESSFUL lookup ran past its deadline
    (campaigns.flag_stale uses last_ok_poll()). An outage never turns real results into wrong finals.
  * A lookup that fails part-way still applies what it already fetched.
  * Sarvam refusing the key for analytics (401/403) pauses new calls, like a refused call would.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .campaigns import apply_result, awaiting_attempts
from .config import settings
from .sarvam import ATTEMPT_BATCH, SarvamError, fetch_attempts, fetch_transcript, webhooks_enabled
from .store import append_log, set_status
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
FINAL = {"connected", "no_answer", "busy", "failed"}
GRACE_MINUTES = 10          # after hang-up, wait this long at most for the agent's outputs and the transcript
NO_END_MINUTES = 30         # a "connected" record with no end time after this long is taken as it is
TRANSCRIPTS_PER_POLL = 20   # the rest wait for the next poll, so one poll can't hold up calls and emails
WEBHOOK_FIRST_MINUTES = 10  # with a public address, the webhook (richer data) gets this long before we fetch
_last_poll: Optional[datetime] = None   # in memory: at most one lookup per RESULT_POLL_SECONDS
_last_ok: Optional[datetime] = None     # when a lookup of ALL waiting calls last succeeded
_seen: set = set()                      # attempt ids Sarvam listed in that lookup (ringing, or waiting for data)


def last_ok_poll() -> Optional[datetime]:
    return _last_ok


def seen_attempts() -> set:
    """Calls Sarvam does know about but that aren't finished/ready yet: not to be timed out early."""
    return _seen


def process_result(payload: dict, source: str) -> str:
    """Apply one call result (from the webhook or from polling). Returns the outcome."""
    append_log("webhooks", {"source": source, "payload": payload})  # raw copy first, for audit and replay
    try:
        outcome = apply_result(payload)
    except Exception as exc:
        log.exception("Applying a call result failed")
        append_log("webhook_errors", {"source": source, "payload": payload, "error": repr(exc)[:1000]})
        return "error"
    set_status("last_webhook", {"at": iso(utcnow()), "outcome": outcome, "via": source})
    return outcome


def parse_utc(text) -> Optional[datetime]:
    """Sarvam timestamps (UTC, maybe 'Z', an offset, or any number of fraction digits) -> naive UTC."""
    if not text:
        return None
    s = str(text).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if m:  # Python 3.10 only accepts 3 or 6 fraction digits
        head, frac, tail = m.groups()
        s = head + (f".{(frac + '000000')[:6]}" if frac else "") + tail
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _status(rec: dict) -> str:
    return str(rec.get("connectivity_status") or "").lower()


def _best(records: list[dict]) -> dict:
    """Several records for one attempt (platform retries, duplicates). If anyone answered and the call
    ended, that conversation wins (the longest one). Otherwise the newest try decides, even while it is
    still in progress, so an early failed try can't hide a later answered one."""
    answered = [r for r in records if _status(r) == "connected" and r.get("end_datetime")]
    if answered:
        return max(answered, key=lambda r: (r.get("duration_in_seconds") or 0, str(r.get("end_datetime"))))
    return max(records, key=lambda r: (r.get("server_retry_attempt") or 0, str(r.get("attempted_at") or "")))


def to_payload(rec: dict, waiting: dict, now: datetime, budget: dict) -> Optional[dict]:
    """A finished Analytics record as a webhook-shaped payload; None = not ready yet, look again later."""
    status = _status(rec)
    if status not in FINAL:
        return None  # still ringing / in progress
    transcript = None
    if status == "connected":
        ended = parse_utc(rec.get("end_datetime"))
        if not ended:  # answered but not hung up yet
            started = parse_utc(rec.get("attempted_at")) or parse_iso(waiting["triggered_at"])
            if now - started < timedelta(minutes=NO_END_MINUTES):
                return None
            ended = started  # never got an end time: take what there is
        fresh = now - ended < timedelta(minutes=GRACE_MINUTES)
        outputs = rec.get("agent_variables") or {}
        if fresh and not any(outputs.get(k) not in (None, "") for k in settings.result_field_map.values()):
            return None  # the agent's answers aren't saved yet
        if rec.get("interaction_id"):
            if budget["transcripts"] <= 0:
                return None  # enough work for this poll; the next one picks it up
            budget["transcripts"] -= 1
            try:
                transcript = fetch_transcript(rec["interaction_id"])
            except SarvamError as exc:
                if fresh:
                    return None
                log.warning("Transcript for %s not available: %s", rec["interaction_id"], exc)
            if fresh and not transcript:
                return None  # not saved yet
    reason = rec.get("failure_reason")
    if reason in (None, "", "NO_FAILURE_REASON"):
        reason = None
    return {
        "attempt_id": rec.get("attempt_id"), "status": status,
        "duration": rec.get("duration_in_seconds") if status == "connected" else None,
        "interaction_id": rec.get("interaction_id"), "failure_reason": reason,
        "final_agent_variables": rec.get("agent_variables") if status == "connected" else None,
        "interaction_transcript": transcript, "audio_url": rec.get("audio_url") if status == "connected" else None,
        "app_id": settings.sarvam_app_id,
        "webhook_config": {"metadata": {"campaign_id": waiting["campaign_id"], "row_id": waiting["row_id"],
                                        "ref": waiting["ref"]}},
    }


def poll_results(force: bool = False) -> dict:
    """One lookup of all calls still waiting for a result. Cheap no-op when nothing is waiting.
    Returns {"waiting", "applied"} plus "auth_error" when Sarvam refused the key (caller pauses calls)."""
    global _last_poll, _last_ok, _seen
    stats: dict = {"waiting": 0, "applied": 0}
    if settings.missing_for_calling():
        return stats
    now = utcnow()
    if not force and _last_poll and (now - _last_poll).total_seconds() < settings.result_poll_seconds:
        return stats
    waiting = awaiting_attempts(now)
    if webhooks_enabled():  # Sarvam posts results itself; fetching is the safety net for ones it didn't deliver
        cutoff = now - timedelta(minutes=WEBHOOK_FIRST_MINUTES)
        waiting = [w for w in waiting if w["status"] == "no_result" or parse_iso(w["triggered_at"]) < cutoff]
    stats["waiting"] = len(waiting)
    if not waiting:
        return stats
    _last_poll = now
    by_id = {str(w["sarvam_attempt_id"]): w for w in waiting}
    ids = list(by_id)
    since = min(parse_iso(w["triggered_at"]) for w in waiting) - timedelta(minutes=15)
    budget = {"transcripts": TRANSCRIPTS_PER_POLL}
    found, error, seen = 0, None, set()
    for i in range(0, len(ids), ATTEMPT_BATCH):
        try:
            records = fetch_attempts(ids[i:i + ATTEMPT_BATCH], since)
        except SarvamError as exc:
            error = exc
            break  # keep what earlier batches found; the rest waits for the next poll
        grouped: dict[str, list[dict]] = {}
        for rec in records:
            if str(rec.get("attempt_id")) in by_id:
                grouped.setdefault(str(rec["attempt_id"]), []).append(rec)
        found += len(grouped)
        seen.update(grouped)
        for attempt_id, recs in grouped.items():
            payload = to_payload(_best(recs), by_id[attempt_id], now, budget)
            if payload and process_result(payload, "poll") == "processed":
                stats["applied"] += 1
    if error is not None:
        log.warning("Fetching call results from Sarvam failed (retrying next poll): %s", error)
        set_status("result_poll", {"at": iso(now), "ok": False, "error": str(error)[:300], "waiting": len(waiting),
                                   "applied": stats["applied"], "last_ok_at": iso(_last_ok)})
        if error.status_code in (401, 403):
            stats["auth_error"] = str(error)
        return stats
    _last_ok, _seen = now, seen
    set_status("result_poll", {"at": iso(now), "ok": True, "waiting": len(waiting), "found": found,
                               "applied": stats["applied"], "last_ok_at": iso(now)})
    return stats
