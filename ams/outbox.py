"""
Results out: the client's SFTP folder gets a "received" file straight away, an update after each finished call
time, and the FINAL file once every call is done (written by ams/dropfolder.py).

send_due_results(): queues any due update/final for each open campaign (campaigns.decide_deliveries), then
writes everything pending. A write cut off by a restart is retried after STUCK_MINUTES, and a FINAL composed
before the campaign was reopened ("Call again", a late result) doesn't close it, so the corrected one follows.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .campaigns import decide_deliveries, delivers
from .config import settings
from .dropfolder import deliver
from .store import campaign_dir, campaign_tx, list_campaign_ids, load_campaign
from .timeutil import iso, parse_iso, stamp, utcnow

log = logging.getLogger(__name__)
STUCK_MINUTES = 10  # still "sending" after this long: cut off (restart, crash), so write it again


def send_due_results() -> int:
    """Queue any due update/final files, then write everything pending. Returns files written."""
    written = 0
    for cid in list_campaign_ids():
        if (campaign_dir(cid) / ".done").exists():
            continue
        with campaign_tx(cid) as camp:
            stuck_before = utcnow() - timedelta(minutes=STUCK_MINUTES)
            for d in camp["deliveries"]:
                if d["status"] == "sending" and parse_iso(d.get("sending_at") or d["created_at"]) < stuck_before:
                    d.update(status="failed", failures=d["failures"] + 1,
                             error="Interrupted while writing (restart?); written again")
            decide_deliveries(camp)
            if not delivers(camp):  # uploaded in the dashboard, or left by the email version: nothing to write
                for d in camp["deliveries"]:
                    if d["status"] in ("pending", "sending", "failed"):
                        d.update(status="skipped", error="Not an SFTP upload: download the results in the dashboard")
            pending = [i for i, d in enumerate(camp["deliveries"])
                       if d["status"] in ("pending", "failed") and d["failures"] < settings.max_delivery_failures]
            for i in pending:
                camp["deliveries"][i]["status"] = "sending"
                camp["deliveries"][i]["sending_at"] = stamp()
        for i in pending:
            written += _send_one(cid, i)
        with campaign_tx(cid) as camp:  # under the lock, so "Call again" can't slip in between check and marker
            if _campaign_closed(camp):
                (campaign_dir(cid) / ".done").touch()
    return written


def _campaign_closed(camp: dict) -> bool:
    if any(d["status"] in ("pending", "sending", "failed") and d["failures"] < settings.max_delivery_failures
           for d in camp["deliveries"]):
        return False
    if camp["status"] == "rejected":
        return True
    return camp["status"] == "completed" and (camp.get("final_sent") or not delivers(camp))


def _send_one(cid: str, index: int) -> int:
    camp = load_campaign(cid)
    rec = camp["deliveries"][index]
    ok, error, saved = False, None, None
    try:
        saved = deliver(camp, rec)
        ok = True
    except Exception as exc:  # keep the loop alive; retried next cycle (the dashboard shows the error)
        log.exception("Writing the %s file for %s to the SFTP folder failed", rec["kind"], cid)
        error = str(exc)[:500]
    with campaign_tx(cid) as camp:
        rec = camp["deliveries"][index]
        rec["file"] = saved or rec.get("file")
        if ok:
            rec.update(status="sent", sent_at=iso(utcnow()), error=None)
            # A final composed before the campaign was reopened is out of date: it doesn't finish the campaign
            if rec["kind"] == "final" and (rec.get("sending_at") or "") > (camp.get("reopened_at") or ""):
                camp["final_sent"] = True
        else:
            rec.update(status="failed", failures=rec["failures"] + 1, error=error)
    return int(ok)
