"""
Call results: fetched from Sarvam, so this server needs NO public address.

Every RESULT_POLL_SECONDS the scheduler calls poll_results(): it asks Sarvam's Analytics API
about the calls still waiting for a result (by the attempt_id Sarvam gave us when the call was
placed), and applies each finished one exactly like a webhook would. If PUBLIC_BASE_URL is set,
Sarvam's webhook also delivers results straight away; whichever arrives first wins and the other
is ignored as a duplicate.

Analytics record -> result
  connectivity_status  connected | no_answer | busy | failed   (anything else = still in progress)
  connected calls count as finished only once they have an end_datetime
  failure_reason       NO_FAILURE_REASON means none
  agent_variables      the agent's outputs (used for connected calls)
  audio_url            Sarvam's signed recording link (opens without a login when the workspace
                       has "Public signed URLs" on; expires after 14 days)
  transcript           fetched separately for connected calls
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from .campaigns import apply_result, awaiting_attempts
from .config import settings
from .sarvam import SarvamError, fetch_attempts, fetch_transcript
from .store import append_log, set_status
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
FINAL = {"connected", "no_answer", "busy", "failed"}
_last_poll = None  # in memory: at most one Sarvam lookup per RESULT_POLL_SECONDS


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


def _best(records: list[dict]) -> dict:
    """Several records for one attempt (platform retries): prefer a finished one, then the latest try."""
    def key(r):
        status = str(r.get("connectivity_status") or "").lower()
        return (status in FINAL, r.get("server_retry_attempt") or 0, str(r.get("attempted_at") or ""))
    return max(records, key=key)


def to_payload(rec: dict, waiting: dict) -> Optional[dict]:
    """A finished Analytics record as a webhook-shaped payload; None while the call is still going."""
    status = str(rec.get("connectivity_status") or "").lower()
    if status not in FINAL:
        return None
    if status == "connected" and not rec.get("end_datetime"):
        return None  # answered but not hung up yet: the agent's outputs aren't final
    reason = rec.get("failure_reason")
    if reason in (None, "", "NO_FAILURE_REASON"):
        reason = None
    transcript = None
    if status == "connected" and rec.get("interaction_id"):
        try:
            transcript = fetch_transcript(rec["interaction_id"])
        except SarvamError as exc:  # the result still counts; only the transcript is missing
            log.warning("Transcript for %s not available: %s", rec["interaction_id"], exc)
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
    """One lookup of all calls still waiting for a result. Cheap no-op when nothing is waiting."""
    global _last_poll
    stats = {"waiting": 0, "applied": 0}
    if settings.missing_for_calling():
        return stats
    now = utcnow()
    if not force and _last_poll and (now - _last_poll).total_seconds() < settings.result_poll_seconds:
        return stats
    waiting = awaiting_attempts(now)
    stats["waiting"] = len(waiting)
    if not waiting:
        return stats
    _last_poll = now
    by_id = {str(w["sarvam_attempt_id"]): w for w in waiting}
    since = min(parse_iso(w["triggered_at"]) for w in waiting) - timedelta(minutes=15)
    try:
        records = fetch_attempts(list(by_id), since)
    except SarvamError as exc:
        log.warning("Fetching call results from Sarvam failed (retrying next cycle): %s", exc)
        set_status("result_poll", {"at": iso(now), "ok": False, "error": str(exc)[:300]})
        return stats
    grouped: dict[str, list[dict]] = {}
    for rec in records:
        if str(rec.get("attempt_id")) in by_id:
            grouped.setdefault(str(rec["attempt_id"]), []).append(rec)
    for attempt_id, recs in grouped.items():
        payload = to_payload(_best(recs), by_id[attempt_id])
        if payload and process_result(payload, "poll") == "processed":
            stats["applied"] += 1
    set_status("result_poll", {"at": iso(now), "ok": True, "waiting": len(waiting), "found": len(grouped),
                               "applied": stats["applied"]})
    return stats
