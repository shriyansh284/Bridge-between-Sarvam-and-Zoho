"""
Background work. Runs inside server.py by default, or on its own:

    python -m ams.scheduler          # loop forever
    python -m ams.scheduler --once   # one pass (testing)

Every SCHEDULER_POLL_SECONDS: flag lost results, place due calls, send due emails.
Every MAIL_POLL_SECONDS: read new emails from the Zoho inbox.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time

from datetime import timedelta

from .campaigns import (claim_row, due_row_ids, flag_stale, refresh_status, trigger_blocked, trigger_failed,
                        trigger_succeeded)
from .config import settings
from .mailer import ensure_mail_cutoff, poll_inbox, send_due_emails
from .sarvam import SarvamError, place_call
from .store import campaign_dir, campaign_tx, get_status, list_campaign_ids, set_status
from .timeutil import fmt_local, iso, next_allowed_time, utcnow

log = logging.getLogger("scheduler")
ACCOUNT_ERRORS = (401, 403)   # Sarvam rejected the API key / org / workspace: no call can succeed
ACCOUNT_PAUSE_MINUTES = 5
_paused_until = None          # in memory, so a restart (after fixing .env) retries straight away


def _pause_calls(reason: str) -> None:
    global _paused_until
    _paused_until = utcnow() + timedelta(minutes=ACCOUNT_PAUSE_MINUTES)
    set_status("calls_paused", {"at": iso(utcnow()), "reason": reason[:400]})
    log.error("Calls paused for %s min: %s", ACCOUNT_PAUSE_MINUTES, reason)


def place_due_calls() -> dict:
    stats = {"placed": 0, "errors": 0, "deferred": 0, "stale": 0}
    if settings.missing_for_calling():
        return stats  # rows keep waiting; the dashboard lists what's missing in .env
    if _paused_until and utcnow() < _paused_until:
        return stats
    budget = settings.max_calls_per_cycle
    for cid in list_campaign_ids():
        if (campaign_dir(cid) / ".done").exists():
            continue
        now = utcnow()
        with campaign_tx(cid) as camp:
            if camp["status"] in ("rejected", "completed"):
                due = []
            else:
                stats["stale"] += flag_stale(camp, now)
                due = due_row_ids(camp, now)
                allowed = next_allowed_time(now)
                if due and allowed > now:  # outside calling hours: move to the next window
                    for row in camp["rows"]:
                        if row["row_id"] in due:
                            row["next_call_at"] = iso(allowed)
                            row["note"] = f"Outside calling hours; moved to {fmt_local(allowed)}"
                    stats["deferred"] += len(due)
                    due = []
                refresh_status(camp)
        for row_id in due:
            if budget <= 0:
                return stats
            job = claim_row(cid, row_id)
            if not job:
                continue
            budget -= 1
            att = job["attempt"]
            metadata = {"campaign_id": cid, "row_id": row_id, "ref": att["ref"]}
            try:
                sarvam_id = place_call(job["phone"], job["agent_variables"], job["app_overrides"], metadata)
            except SarvamError as exc:
                if exc.status_code in ACCOUNT_ERRORS:
                    trigger_blocked(cid, row_id, att["ref"], "Sarvam rejected the API key or workspace IDs; "
                                                             "calls are paused until it's fixed")
                    _pause_calls(str(exc))
                    stats["errors"] += 1
                    return stats
                trigger_failed(cid, row_id, att["ref"], str(exc), exc.retryable)
                stats["errors"] += 1
                log.warning("%s %s not placed: %s", cid, row_id, exc)
                continue
            trigger_succeeded(cid, row_id, att["ref"], sarvam_id)
            stats["placed"] += 1
            if get_status().get("calls_paused"):
                set_status("calls_paused", None)
            log.info("%s %s call placed to %s (sarvam %s)", cid, row_id, job["phone"], sarvam_id)
    return stats


def run_cycle() -> dict:
    stats = place_due_calls()
    stats["emails"] = send_due_emails()
    set_status("scheduler_heartbeat", iso(utcnow()))
    return stats


class Background:
    """Two daemon threads: calls/emails loop and inbox loop."""

    def __init__(self):
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _loop(self, fn, every: int, name: str):
        while not self._stop.is_set():
            try:
                result = fn()
                if isinstance(result, dict) and any(result.values()):
                    log.info("%s: %s", name, result)
            except Exception:
                log.exception("%s failed; retrying next cycle", name)
            self._stop.wait(every)

    def start(self):
        set_status("calls_paused", None)  # a restart picks up a fixed .env
        ensure_mail_cutoff()
        missing = settings.missing_for_calling()
        if missing:
            log.warning("Calls can't be placed until these are set (rows keep waiting): %s", ", ".join(missing))
        if settings.mail_enabled and settings.missing_for_mail():
            log.warning("Inbox can't be read until these are set: %s", ", ".join(settings.missing_for_mail()))
        for fn, every, name in ((run_cycle, settings.scheduler_poll_seconds, "scheduler"),
                                (poll_inbox, settings.mail_poll_seconds, "inbox")):
            t = threading.Thread(target=self._loop, args=(fn, every, name), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.once:
        ensure_mail_cutoff()
        log.info("inbox: %s", poll_inbox())
        log.info("cycle: %s", run_cycle())
        return
    bg = Background()
    bg.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        bg.stop()


if __name__ == "__main__":
    main()
