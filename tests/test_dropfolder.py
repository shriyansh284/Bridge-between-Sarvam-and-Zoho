"""
Client files through the SFTP folder (ams/dropfolder.py), against mock Sarvam (no real calls):

inbox/<file> -> send/<file>_<campaign>_RESULTS.csv -> ..._FINAL.csv, candidates not reached ->
failed/<file>_<campaign>_MISSED_CALLS.csv, bad files -> failed/ with a note.
"""
import csv
import io
import os
import time
from datetime import datetime, timedelta

import pytest

from ams import dropfolder, scheduler
from ams.campaigns import TERMINAL
from ams.config import settings
from ams.sheet import detect_date_order, read_sheet
from ams.store import campaign_dir, campaign_tx, list_campaign_ids, load_campaign, read_log
from ams.timeutil import LOCAL_TZ, iso, utcnow

pytestmark = pytest.mark.usefixtures("servers")
HEADER = "Candidate Name,Phone Number,Hiring Company,Pending Documents,WhatsApp Message Date,Call Date,Call Time"


@pytest.fixture
def drop(tmp_path):
    keys = ("drop_dir", "drop_settle_seconds", "drop_result_format", "drop_date_order")
    before = {k: getattr(settings, k) for k in keys}
    root = tmp_path / "sftp"
    root.mkdir()
    for key, value in (("drop_dir", str(root)), ("drop_settle_seconds", 0), ("drop_result_format", "csv"),
                       ("drop_date_order", "dmy")):
        object.__setattr__(settings, key, value)  # Settings is frozen; tests only
    yield root
    for key, value in before.items():
        object.__setattr__(settings, key, value)


def _upload(root, name, text: str):
    (root / "inbox").mkdir(exist_ok=True)
    (root / "inbox" / name).write_bytes(text.encode("utf-8-sig"))


def _campaign_for(name: str) -> str:
    return next(c for c in list_campaign_ids() if ((load_campaign(c).get("drop") or {}).get("file") == name))


def _csv_by(path, key: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    return {r[key]: r for r in rows}


def _csv(path) -> dict:
    return _csv_by(path, "Candidate Name")


def _run_until(predicate, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        scheduler.run_cycle()
        if predicate():
            return
        time.sleep(0.4)
    raise AssertionError("condition not reached")


def test_sftp_file_to_final_result(drop):
    later = datetime.now(LOCAL_TZ) + timedelta(hours=2)
    _upload(drop, "batch_26Nov.csv", "\n".join([
        HEADER + ",Client Ref",  # an extra column of the client's own comes back unchanged
        "Asha Verma,9876511110,ABC Ltd,\"PAN, Degree\",25-11-2026,,NOW,ref-1",          # connected
        "Ravi Kumar,9876511111,XYZ Pvt,Aadhaar,25-11-2026,,NOW,ref-2",                    # no answer
        "Bad Number,12345,XYZ Pvt,Aadhaar,25-11-2026,,NOW,ref-3",                          # not called
        f"Later Person,9876511112,XYZ Pvt,Aadhaar,25-11-2026,{later:%d-%m-%Y},{later:%I:%M %p},ref-4",  # busy
    ]) + "\n")
    _upload(drop, "next.csv.filepart", HEADER + "\n")  # still uploading under a temporary name
    _upload(drop, ".hidden.csv", HEADER + "\n")
    object.__setattr__(settings, "drop_result_format", "both")

    stats = dropfolder.poll_drop()
    assert stats["campaigns"] == 1
    assert sorted(p.name for p in (drop / "inbox").iterdir()) == [".hidden.csv", "next.csv.filepart"]  # picked up
    cid = _campaign_for("batch_26Nov.csv")
    camp = load_campaign(cid)
    assert camp["source"] == "sftp" and (campaign_dir(cid) / "input.csv").exists()  # the original is kept here
    assert any(e.get("from") == "SFTP upload" and e.get("campaign") == cid for e in read_log("file_log"))

    # the "received" file appears straight away: the schedule, and the row that can't be called
    scheduler.run_cycle()
    base = f"batch_26Nov_{cid}"
    got = _csv(drop / "send" / f"{base}_RESULTS.csv")
    assert got["Bad Number"]["Call Status"] == "Not called (invalid row)" and "12345" in got["Bad Number"]["Remarks"]
    assert got["Later Person"]["Remarks"].startswith("Call scheduled for")
    assert (drop / "send" / f"{base}_RESULTS.xlsx").exists()
    assert list(_csv(drop / "failed" / f"{base}_MISSED_CALLS.csv")) == ["Bad Number"]  # not called: a fault

    def now_slot_done():
        return all(r["status"] in TERMINAL for r in load_campaign(cid)["rows"] if r["slot"] == "Immediate")
    _run_until(now_slot_done)
    scheduler.run_cycle()
    got = _csv(drop / "send" / f"{base}_RESULTS.csv")
    assert got["Asha Verma"]["Call Status"] == "Connected" and got["Asha Verma"]["Candidate Response"] == "Agreed to submit"
    assert got["Asha Verma"]["Recording Link"].startswith("http")
    assert got["Ravi Kumar"]["Call Status"] == "No Answer" and got["Ravi Kumar"]["Attempt Count"] == "2"
    missed = _csv(drop / "failed" / f"{base}_MISSED_CALLS.csv")
    assert sorted(missed) == ["Bad Number", "Ravi Kumar"] and missed["Ravi Kumar"]["Client Ref"] == "ref-2"

    with campaign_tx(cid) as c:  # the later call time comes round
        for r in c["rows"]:
            if r["status"] == "scheduled":
                r["next_call_at"] = iso(utcnow())
    _run_until(lambda: load_campaign(cid)["final_sent"])
    assert not (drop / "send" / f"{base}_RESULTS.csv").exists()  # replaced by the final file
    final = _csv(drop / "send" / f"{base}_FINAL.csv")
    assert final["Later Person"]["Call Status"] == "Busy"
    assert final["Asha Verma"]["Pending Documents"] == "PAN, Degree" and final["Asha Verma"]["Client Ref"] == "ref-1"
    header = (drop / "send" / f"{base}_FINAL.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert header[:8] == HEADER.split(",") + ["Client Ref"] and header[8] == "Call Status"
    assert (drop / "send" / f"{base}_FINAL.xlsx").exists()
    assert sorted(_csv(drop / "failed" / f"{base}_MISSED_CALLS.csv")) == ["Bad Number", "Later Person", "Ravi Kumar"]
    assert (drop / "failed" / f"{base}_MISSED_CALLS.xlsx").exists()
    assert not [p.name for d in ("send", "failed") for p in (drop / d).iterdir() if p.name.startswith(".")]
    assert (campaign_dir(cid) / ".done").exists()
    assert all(d["status"] == "sent" for d in load_campaign(cid)["deliveries"])


def test_file_still_uploading_is_left_alone(drop):
    _upload(drop, "slow.csv", HEADER + "\nSlow Upload,9876511120,ABC,PAN,25-11-2026,,NOW\n")
    object.__setattr__(settings, "drop_settle_seconds", 60)
    assert dropfolder.poll_drop() == {"files": 0, "campaigns": 0, "rejected": 0, "waiting": 1}
    assert (drop / "inbox" / "slow.csv").exists()
    object.__setattr__(settings, "drop_settle_seconds", 0)  # a minute later: unchanged, so the upload is done
    assert dropfolder.poll_drop()["campaigns"] == 1


def test_unusable_files_go_to_failed_with_a_note(drop):
    _upload(drop, "no_header.csv", "Asha Verma,9876511130,ABC Ltd,PAN,25-11-2026,,NOW\n")  # data only, no header
    _upload(drop, "notes.pdf", "not a spreadsheet")
    stats = dropfolder.poll_drop()
    assert stats["rejected"] == 2 and stats["campaigns"] == 0
    assert not list((drop / "inbox").iterdir())
    note = (drop / "failed" / "no_header.csv_ERROR.txt").read_text(encoding="utf-8-sig")
    assert "No calls were placed" in note and "header" in note and "Pending Documents (required)" in note
    assert (drop / "failed" / "no_header.csv").exists()
    assert "Only .csv and .xlsx" in (drop / "failed" / "notes.pdf_ERROR.txt").read_text(encoding="utf-8-sig")


def test_same_file_twice_is_not_called_again(drop):
    text = HEADER + "\nTwice Uploaded,9876511140,ABC,PAN,25-11-2026,,NOW\n"
    _upload(drop, "monday.csv", text)
    assert dropfolder.poll_drop()["campaigns"] == 1
    _upload(drop, "monday.csv", text)  # same name and content again
    assert dropfolder.poll_drop()["rejected"] == 1
    assert (drop / "failed" / "monday.csv").exists() and not list((drop / "inbox").iterdir())
    note = (drop / "failed" / "monday.csv_ERROR.txt").read_text(encoding="utf-8-sig")
    assert "already received as campaign" in note


def test_no_row_callable_gives_an_errors_file(drop):
    _upload(drop, "all_bad.csv", HEADER + "\nNo Phone,,ABC,PAN,25-11-2026,,NOW\n")
    dropfolder.poll_drop()
    cid = _campaign_for("all_bad.csv")
    scheduler.run_cycle()
    assert load_campaign(cid)["status"] == "rejected"
    note = (drop / "failed" / f"all_bad_{cid}_ERRORS.txt").read_text(encoding="utf-8-sig")
    assert "Phone Number is empty" in note
    assert _csv(drop / "failed" / f"all_bad_{cid}_ERRORS.csv")["No Phone"]["Remarks"] == "Phone Number is empty"
    assert not list((drop / "send").glob(f"*{cid}*"))  # nothing in send/ for it


def test_restart_in_the_middle_of_a_pickup_never_makes_two_campaigns(drop):
    (drop / "inbox").mkdir(exist_ok=True)
    (drop / "inbox" / ".picked-crash-crash.csv").write_bytes(
        (HEADER + "\nCrash Test,9876511150,ABC,PAN,25-11-2026,,NOW\n").encode("utf-8-sig"))
    # claimed (renamed to its hidden name), then the server stopped before the campaign was created
    dropfolder._record("claim-crash", file="crash.csv", claimed_as=".picked-crash-crash.csv", status="claimed",
                       at=iso(utcnow()))
    assert dropfolder.poll_drop()["campaigns"] == 1
    assert not list((drop / "inbox").iterdir())  # the claimed file is gone once its campaign exists
    # the campaign was created, then the server stopped before the pickup was marked done
    dropfolder._record("claim-crash", status="claimed")
    dropfolder.poll_drop()
    assert sum(1 for c in list_campaign_ids() if (load_campaign(c).get("drop") or {}).get("claim_id") == "claim-crash") == 1
    # stopped after the record but before the file left the inbox: picked up normally
    _upload(drop, "late.csv", HEADER + "\nLate Pickup,9876511151,ABC,PAN,25-11-2026,,NOW\n")
    dropfolder._record("claim-late", file="late.csv", claimed_as=".picked-late-late.csv", status="claiming",
                       at=iso(utcnow()))
    assert dropfolder.poll_drop()["campaigns"] == 1
    assert "claim-late" not in dropfolder._claims()


FORM = ("AI Call Agent Persona,AI Call Category,Person to Call,Reason for Calling,Call on Behalf of,"
        "WhatsApp Message Date,Phone Number to Call,Date to Call,Time to Call")  # the client's own form


def test_clients_form_export_is_fed_to_the_agent_and_called_at_each_rows_time(drop):
    now = datetime.now(LOCAL_TZ)
    later = now + timedelta(minutes=45)  # uploaded around 10:30, one record booked for 11:15 the same day
    form_time = f"{now:%I:%M%p}".lower()  # as the form writes it: 05:00pm
    rows = [
        f"Kajal,Pending documents,Meera Shah,\"PAN Card, Degree\",ABC Ltd,11/25/2026,+91 98765 11160,"
        f"{now:%m/%d/%Y},{form_time}",
        f"Kajal,Pending documents,Arjun Rao,Aadhaar,XYZ Pvt,11/25/2026,9876511162,{later:%m/%d/%Y},{later:%I:%M %p}",
        f"Kajal,Pending documents,No Reason,,XYZ Pvt,11/25/2026,9876511163,{later:%m/%d/%Y},{later:%I:%M %p}",
    ]
    _upload(drop, "form_export.csv", FORM + "\n" + "\n".join(rows) + "\n")
    assert dropfolder.poll_drop()["campaigns"] == 1  # 11/25/2026 shows this file is month first: no setting needed
    cid = _campaign_for("form_export.csv")
    got = {r["name"]: r for r in load_campaign(cid)["rows"]}

    meera = got["Meera Shah"]  # exactly what the agent is given
    assert meera["agent_variables"] == {"name": "Meera Shah", "phone_number": "9876511160",
                                        "Name_of_Hiring_company": "ABC Ltd", "pending_document_names": "PAN Card, Degree",
                                        "whatsapp_message_date": "25-11-2026"}
    assert meera["phone"] == "+919876511160" and meera["status"] == "scheduled"
    arjun = got["Arjun Rao"]
    assert arjun["slot"] == f"{later:%Y-%m-%d %H:%M}" and arjun["next_call_at"] > meera["next_call_at"]
    assert got["No Reason"]["status"] == "invalid" and "Reason for Calling is empty" in got["No Reason"]["note"]

    _run_until(lambda: load_campaign(cid)["rows"][0]["status"] in TERMINAL)  # the first time is due now
    assert next(r for r in load_campaign(cid)["rows"] if r["name"] == "Arjun Rao")["status"] == "scheduled"
    with campaign_tx(cid) as c:  # 11:15 comes round
        for r in c["rows"]:
            if r["status"] == "scheduled":
                r["next_call_at"] = iso(utcnow())
    _run_until(lambda: load_campaign(cid)["final_sent"])
    final = _csv_by(drop / "send" / f"form_export_{cid}_FINAL.csv", "Person to Call")
    assert final["Meera Shah"]["Call Status"] == "Connected" and final["Arjun Rao"]["Call Status"] == "Busy"
    assert final["Meera Shah"]["AI Call Agent Persona"] == "Kajal"  # their columns come back unchanged


def test_dates_are_read_the_way_each_file_writes_them(drop):
    later = datetime.now(LOCAL_TZ) + timedelta(days=3)
    day_first = f"Kajal,Pending documents,Day First,PAN,ABC,25/11/2026,9876511164,{later:%d/%m/%Y},05:00pm"
    _upload(drop, "day_first.csv", FORM + "\n" + day_first + "\n")
    object.__setattr__(settings, "drop_date_order", "mdy")  # the client's usual order, but this file shows its own
    assert dropfolder.poll_drop()["campaigns"] == 1
    assert load_campaign(_campaign_for("day_first.csv"))["rows"][0]["slot"] == f"{later:%Y-%m-%d} 17:00"

    mixed = [f"Kajal,Pending documents,Mixed One,PAN,ABC,11/25/2026,9876511165,{later:%m/%d/%Y},10:00",
             f"Kajal,Pending documents,Mixed Two,PAN,ABC,25/11/2026,9876511166,{later:%m/%d/%Y},10:00"]
    _upload(drop, "mixed.csv", FORM + "\n" + "\n".join(mixed) + "\n")
    assert dropfolder.poll_drop()["rejected"] == 1
    assert "written two ways" in (drop / "failed" / "mixed.csv_ERROR.txt").read_text(encoding="utf-8-sig")


def test_a_record_sent_again_in_a_later_file_is_not_called_twice(drop):
    later = datetime.now(LOCAL_TZ) + timedelta(hours=3)
    when = f"{later:%m/%d/%Y},{later:%I:%M %p}"
    _upload(drop, "batch_10am.csv", FORM + f"\nKajal,Pending documents,Repeat Person,PAN,ABC,11/25/2026,9876511167,{when}\n")
    dropfolder.poll_drop()
    first = _campaign_for("batch_10am.csv")
    _upload(drop, "batch_11am.csv", FORM + "\n"  # their next export repeats the record and adds a new one
            f"Kajal,Pending documents,Repeat Person,PAN,ABC,11/25/2026,9876511167,{when}\n"
            f"Kajal,Pending documents,New Person,PAN,ABC,11/25/2026,9876511168,{when}\n")
    dropfolder.poll_drop()
    got = {r["name"]: r for r in load_campaign(_campaign_for("batch_11am.csv"))["rows"]}
    assert got["Repeat Person"]["status"] == "invalid" and first in got["Repeat Person"]["note"]
    assert got["New Person"]["status"] == "scheduled"


def test_a_file_that_cannot_be_read_never_blocks_the_others(drop, monkeypatch):
    real_create = dropfolder.create_campaign

    def reader_crash(data, name, **kw):  # e.g. a damaged workbook the spreadsheet library chokes on
        if name == "broken.csv":
            raise ValueError("unexpected parser failure")
        return real_create(data, name, **kw)
    monkeypatch.setattr(dropfolder, "create_campaign", reader_crash)
    _upload(drop, "broken.csv", HEADER + "\nBroken File,9876511190,ABC,PAN,25-11-2026,,NOW\n")
    _upload(drop, "fine.csv", HEADER + "\nFine File,9876511191,ABC,PAN,25-11-2026,,NOW\n")
    stats = dropfolder.poll_drop()
    assert stats["campaigns"] == 1 and stats["rejected"] == 1
    assert "couldn't be read" in (drop / "failed" / "broken.csv_ERROR.txt").read_text(encoding="utf-8-sig")

    def no_permission(path):  # uploaded as 600: only the client's login may read it
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(dropfolder, "_read", no_permission)
    _upload(drop, "private.csv", HEADER + "\nPrivate File,9876511192,ABC,PAN,25-11-2026,,NOW\n")
    assert dropfolder.poll_drop()["rejected"] == 1
    assert "660" in (drop / "failed" / "private.csv_ERROR.txt").read_text(encoding="utf-8-sig")
    assert dropfolder.poll_drop() == {"files": 0, "campaigns": 0, "rejected": 0, "waiting": 0}  # nothing stuck


def test_campaigns_not_from_the_folder_never_write_into_it(drop):
    from ams.campaigns import create_campaign
    from ams.outbox import send_due_results

    camp = create_campaign((HEADER + "\nDash Upload,9876511195,ABC,PAN,25-11-2026,,NOW\n").encode(), "dash.csv",
                           source="dashboard", created_by="tester")
    with campaign_tx(camp["campaign_id"]) as c:  # e.g. an email left queued by the earlier email version
        c["deliveries"].append({"key": "ack", "kind": "ack", "status": "pending", "failures": 0,
                                "created_at": iso(utcnow())})
    send_due_results()
    assert load_campaign(camp["campaign_id"])["deliveries"][0]["status"] == "skipped"
    assert not list((drop / "send").glob(f"*{camp['campaign_id']}*"))


def test_results_are_never_written_outside_the_sftp_folder(drop):
    _upload(drop, "moved.csv", HEADER + "\nMoved Folder,9876511180,ABC,PAN,25-11-2026,,NOW\n")
    dropfolder.poll_drop()
    cid = _campaign_for("moved.csv")
    object.__setattr__(settings, "drop_dir", "")  # DROP_DIR removed from .env while the campaign runs
    scheduler.run_cycle()
    ack = load_campaign(cid)["deliveries"][0]
    assert ack["status"] == "failed" and "DROP_DIR" in ack["error"]  # shown in the dashboard, retried later
    assert not any(os.path.exists(d) for d in ("inbox", "send", "failed"))


def test_date_order_detection():
    def sheet(date):
        return read_sheet((HEADER + f"\nA,9876511170,ABC,PAN,{date},,NOW\n").encode(), "x.csv")
    assert detect_date_order(sheet("11/25/2026"), "dmy") == "mdy"   # 25 can't be a month
    assert detect_date_order(sheet("25/11/2026"), "mdy") == "dmy"
    assert detect_date_order(sheet("05/11/2026"), "mdy") == "mdy"   # could be either: the setting decides
    assert detect_date_order(sheet("05/11/2026"), "dmy") == "dmy"
    assert detect_date_order(sheet("2026-11-25"), "mdy") == "mdy"   # ISO reads the same either way
