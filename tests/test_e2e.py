"""
Whole flow against mock Sarvam (no real calls), through the client's SFTP folder:

    python -m pytest -q

file in inbox/ -> campaign -> calls at each row's time -> results fetched from (or posted by) Sarvam ->
send/<file>_<campaign>_RESULTS after each finished time slot -> ..._FINAL, failed/..._MISSED_CALLS, plus the
safety rules
(Sarvam outages, unconfirmed calls, "Call again", a write cut off by a restart).
"""
import csv
import io
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from openpyxl import Workbook, load_workbook

from ams import dropfolder, outbox, scheduler
from ams.campaigns import TERMINAL
from ams.config import settings
from ams.store import campaign_dir, campaign_tx, list_campaign_ids, load_campaign
from ams.timeutil import LOCAL_TZ, iso, stamp, utcnow
from tools import mock_sarvam

pytestmark = pytest.mark.usefixtures("servers")  # mock Sarvam and the backend (tests/conftest.py)

# The template's 7 columns plus optional extras a client may add (their own ID, Language, notes)
TEST_HEADERS = ["Candidate ID", "Candidate Name", "Phone Number", "Hiring Company", "Pending Documents",
                "WhatsApp Message Date", "Call Date", "Call Time", "Language", "Additional Context"]


def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append(TEST_HEADERS)
    for r, values in enumerate(rows, start=2):
        for c, v in enumerate(values, start=1):
            ws.cell(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _folder(name: str) -> Path:
    return Path(settings.drop_dir) / name


def _upload(data: bytes, name: str) -> str:
    """The client uploads a file; returns the campaign it became."""
    _folder("inbox").mkdir(parents=True, exist_ok=True)
    (_folder("inbox") / name).write_bytes(data)
    assert dropfolder.poll_drop()["campaigns"] == 1
    return next(c for c in list_campaign_ids() if (load_campaign(c).get("drop") or {}).get("file") == name)


def _result(cid: str, label: str, ext: str = "csv") -> Path:
    folder = "failed" if label == "MISSED_CALLS" else "send"
    return _folder(folder) / f"{dropfolder.result_base(load_campaign(cid))}_{label}.{ext}"


def _csv_rows(path: Path) -> dict:
    return {r["Candidate ID"]: r for r in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))}


def _run_until(predicate, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        scheduler.run_cycle()
        if predicate():
            return
        time.sleep(0.4)
    raise AssertionError("condition not reached")


def _rows(ws):
    header = [c.value for c in ws[1]]
    return header, {r[0]: dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True) if r[0]}


def test_excel_file_end_to_end():
    later = datetime.now(LOCAL_TZ) + timedelta(hours=2)
    rows = [  # last digit drives the mock: 0 connected, 1 no answer, 2 busy, 3 failed
        ["C1", "Deep", "9289309920", "ABC", "PAN, Degree", "18-11-2026", "", "NOW", "Hindi", "keep me"],
        ["C2", "Ashu", "8929346531", "Google", "ID proof", "18-11-2026", "", "NOW", "", ""],
        ["C3", "Pranav", "9958007323", "Infosys", "Marksheet", "18-11-2026", "", "NOW", "", ""],
        ["C4", "Broken", "12345", "DXC", "Passport", "18-11-2026", "", "NOW", "", ""],
        ["C5", "Sweta", "9389270380", "Maruti", "Consent form", "18-11-2026", later.strftime("%d-%m-%Y"),
         later.strftime("%I:%M %p"), "", ""],
        ["C6", "Rahul", "9205558162", "Birla", "Address proof", "18-11-2026", later.strftime("%d-%m-%Y"),
         later.strftime("%I:%M %p"), "", ""],
    ]
    object.__setattr__(settings, "drop_result_format", "xlsx")  # Settings is frozen; tests only
    try:
        cid = _upload(_xlsx(rows), "Nov_batch.xlsx")

        # the "received" file: the schedule, and the row that can't be called
        scheduler.run_cycle()
        _, got = _rows(load_workbook(_result(cid, "RESULTS", "xlsx"))["Candidates"])
        assert got["C4"]["Call Status"] == "Not called (invalid row)" and "12345" in got["C4"]["Remarks"]

        # first time slot (NOW) finishes -> the results file is updated
        def slot_now_done():
            return all(r["status"] in TERMINAL for r in load_campaign(cid)["rows"] if r["slot"] == "Immediate")
        _run_until(slot_now_done)
        scheduler.run_cycle()
        assert any(d["kind"] == "update" and d["status"] == "sent" for d in load_campaign(cid)["deliveries"])
        wb = load_workbook(_result(cid, "RESULTS", "xlsx"))
        header, got = _rows(wb["Candidates"])
        assert header[:10] == TEST_HEADERS
        assert got["C1"]["Call Status"] == "Connected" and got["C1"]["Candidate Response"] == "Agreed to submit"
        assert got["C1"]["Commitment & Timeline"] == "Will upload tomorrow" and got["C1"]["Call Quality"] == "Positive"
        assert "Next Follow-up Date" not in header and not got["C1"]["Other Agent Outputs"]  # nothing left unmapped
        assert got["C1"]["Additional Context"] == "keep me"
        assert got["C2"]["Call Status"] == "No Answer" and got["C2"]["Attempt Count"] == 2
        assert got["C3"]["Call Status"] == "Failed" and "NDNC" in got["C3"]["Remarks"]
        assert got["C5"]["Call Status"] == "Scheduled"

        # the recording link plays without any login
        link = wb["Candidates"].cell(2, header.index("Recording Link") + 1).hyperlink.target
        audio = requests.get(link)
        assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
        assert requests.get(link[:-3] + "xyz").status_code == 404  # tampered link

        # make the later slot due now, then the FINAL file replaces the in-progress one
        with campaign_tx(cid) as camp:
            for r in camp["rows"]:
                if r["status"] == "scheduled":
                    r["next_call_at"] = iso(utcnow())
        _run_until(lambda: load_campaign(cid)["final_sent"])
        assert not _result(cid, "RESULTS", "xlsx").exists()
        _, got = _rows(load_workbook(_result(cid, "FINAL", "xlsx"))["Candidates"])
        assert got["C5"]["Call Status"] == "Connected" and got["C6"]["Call Status"] == "Busy"
        assert (campaign_dir(cid) / ".done").exists()

        written = len(load_campaign(cid)["deliveries"])
        scheduler.run_cycle()
        assert len(load_campaign(cid)["deliveries"]) == written  # nothing is written twice
    finally:
        object.__setattr__(settings, "drop_result_format", "csv")


def test_predefined_csv_template_filled_in_by_the_client():
    from ams.sheet import TEMPLATE_HEADERS

    template = open("templates/AMS_Calling_Template.csv", encoding="utf-8-sig").read()
    assert template.strip().split(",") == TEMPLATE_HEADERS  # the template file matches the code
    later = (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(hour=11, minute=0)
    rows = [  # Excel-saved CSV quirks included: dates with slashes, a date next to NOW, a number Excel broke
        f"Asha Verma,9876500051,ABC Ltd,\"PAN, Degree\",18-11-2026,{later:%d-%m-%Y},11:00 AM",
        "Ravi Kumar,9876500050,XYZ Pvt,Aadhaar,18/11/2026,20-11-2026,NOW",
        "Broken Number,9.19877E+11,XYZ Pvt,Aadhaar,18-11-2026,,NOW",
        "No Message Date,9876500052,XYZ Pvt,Aadhaar,,,NOW",
    ]
    cid = _upload((template.strip() + "\n" + "\n".join(rows) + "\n").encode("utf-8-sig"), "client_data.csv")
    row = {r["name"]: r for r in load_campaign(cid)["rows"]}
    asha = row["Asha Verma"]
    assert asha["status"] == "scheduled" and asha["slot"] == f"{later:%Y-%m-%d} 11:00"
    assert asha["agent_variables"] == {  # exactly the agent's 5 variables, all filled
        "name": "Asha Verma", "phone_number": "9876500051", "Name_of_Hiring_company": "ABC Ltd",
        "pending_document_names": "PAN, Degree", "whatsapp_message_date": "18-11-2026"}
    ravi = row["Ravi Kumar"]
    assert ravi["status"] == "scheduled" and ravi["slot"] == "Immediate"           # NOW beats the date
    assert ravi["agent_variables"]["whatsapp_message_date"] == "18-11-2026"         # 18/11/2026 normalised
    assert row["Broken Number"]["status"] == "invalid" and "not a valid mobile" in row["Broken Number"]["note"]
    assert row["No Message Date"]["status"] == "invalid" and "WhatsApp Message Date is empty" in \
        row["No Message Date"]["note"]
    with campaign_tx(cid) as camp:  # don't leave calls waiting for the next tests
        for r in camp["rows"]:
            if r["status"] == "scheduled":
                r["status"] = "cancelled"


def test_webhook_security_and_duplicates():
    r = requests.post("http://127.0.0.1:18000/webhooks/sarvam/wrong", json={})
    assert r.status_code == 404
    r = requests.post("http://127.0.0.1:18000/webhooks/sarvam/hook-secret", json={"attempt_id": "nope", "status": "connected"})
    assert r.json()["outcome"] == "unmatched"


def test_calls_wait_instead_of_failing_when_sarvam_is_not_usable(monkeypatch):
    from ams.sarvam import SarvamError
    from ams.store import get_status

    cid = _upload(_xlsx([["P1", "Kiran", "9876500030", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]]), "paused.xlsx")
    row = lambda: load_campaign(cid)["rows"][0]  # noqa: E731

    # settings missing in .env: nothing is dialled, the row just waits
    monkeypatch.setattr(type(scheduler.settings), "missing_for_calling", lambda self: ["SARVAM_CONNECTION_ID"])
    assert scheduler.place_due_calls()["placed"] == 0 and row()["status"] == "scheduled"
    monkeypatch.undo()

    # Sarvam rejects the API key: the claim is undone, calls pause, the candidate is NOT marked failed
    def rejected(*args, **kwargs):
        raise SarvamError("Sarvam API 401: Invalid API key format.", retryable=False, status_code=401)
    monkeypatch.setattr(scheduler, "place_call", rejected)
    assert scheduler.place_due_calls()["errors"] == 1
    assert row()["status"] == "scheduled" and row()["attempts"] == [] and "API key" in row()["note"]
    assert "401" in get_status()["calls_paused"]["reason"]
    assert scheduler.place_due_calls()["errors"] == 0  # paused: no further attempts for a while

    # key fixed + backend restarted: the waiting call goes out normally
    monkeypatch.undo()
    scheduler._paused_until = None
    _run_until(lambda: row()["status"] == "completed")
    assert row()["attempt_count"] == 1 and not get_status().get("calls_paused")


def test_no_public_address_results_are_fetched_from_sarvam():
    """The production setup: no PUBLIC_BASE_URL, so Sarvam can't post results; the app fetches them."""
    from ams.store import get_status

    original = settings.public_base_url
    object.__setattr__(settings, "public_base_url", "")
    try:
        cid = _upload(_xlsx([["Q1", "Meera", "9876500070", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""],   # connects
                             ["Q2", "Kabir", "9876500072", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""],   # busy twice
                             ["Q3", "Isha", "9876500073", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]]),  # fails
                      "no_public_address.xlsx")
        _run_until(lambda: load_campaign(cid)["final_sent"], seconds=45)

        sent = [b for b in mock_sarvam.CALLS if b["user_config"]["user_phone_number"] in
                ("+919876500070", "+919876500072", "+919876500073")]
        assert len(sent) == 4 and not any("webhook_config" in b for b in sent)  # nothing to post back to
        rows = {r["candidate_id"]: r for r in load_campaign(cid)["rows"]}
        meera = rows["Q1"]["attempts"][-1]
        assert rows["Q1"]["status"] == "completed" and meera["variables"]["disposition_call_disposition"] == "agreed_to_submit"
        assert meera["transcript"][0] == {"role": "agent", "text": "Hello, am I speaking with Meera?", "language": "English"}
        assert meera["audio_url"].startswith("https://apps.sarvam.ai/api/analytics/public/")
        assert rows["Q2"]["status"] == "not_reachable" and rows["Q2"]["attempt_count"] == 2
        assert rows["Q3"]["status"] == "failed" and rows["Q3"]["note"] == "Call could not be placed"
        assert get_status()["last_webhook"]["via"] == "poll" and get_status()["result_poll"]["ok"]

        got = _csv_rows(_result(cid, "FINAL"))
        assert got["Q1"]["Candidate Response"] == "Agreed to submit" and got["Q2"]["Call Status"] == "Busy"
        assert "Agent: Hello, am I speaking with Meera?" in got["Q1"]["Transcript"]
        assert got["Q1"]["Recording Link"] == meera["audio_url"]  # Sarvam's signed link, not this server
        assert sorted(_csv_rows(_result(cid, "MISSED_CALLS"))) == ["Q2", "Q3"]  # busy twice, failed
    finally:
        object.__setattr__(settings, "public_base_url", original)


def test_sarvam_outage_never_turns_real_results_into_a_wrong_final(monkeypatch):
    """Results can't be read for an hour: nothing is given up. Then the call isn't listed yet, so it goes
    to review and a final goes out. Then it turns up: the row is corrected and a corrected final follows."""
    from ams import results
    from ams.sarvam import SarvamError

    original = settings.public_base_url
    object.__setattr__(settings, "public_base_url", "")  # no webhook: results only by fetching
    results._last_ok, results._seen = None, set()
    real_fetch = results.fetch_attempts
    try:
        def down(ids, since):
            raise SarvamError("Sarvam API 503: unavailable", retryable=True, status_code=503)
        monkeypatch.setattr(results, "fetch_attempts", down)
        cid = _upload(_xlsx([["L1", "Lata", "9876500080", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]]), "late.xlsx")
        _run_until(lambda: (load_campaign(cid)["rows"][0]["attempts"] or [{}])[-1].get("sarvam_attempt_id"))
        time.sleep(1)  # the call has finished at (mock) Sarvam
        with campaign_tx(cid) as camp:  # pretend it was placed an hour ago
            camp["rows"][0]["attempts"][-1]["triggered_at"] = iso(utcnow() - timedelta(minutes=60))
        for _ in range(3):
            scheduler.run_cycle()
        camp = load_campaign(cid)
        assert camp["rows"][0]["status"] == "calling" and not camp["final_sent"]  # outage: nothing given up

        monkeypatch.setattr(results, "fetch_attempts", lambda ids, since: [])  # back, but not listing it yet
        _run_until(lambda: load_campaign(cid)["final_sent"])
        assert load_campaign(cid)["rows"][0]["status"] == "needs_review"
        assert _csv_rows(_result(cid, "FINAL"))["L1"]["Call Status"] == "Result pending (under review)"
        assert list(_csv_rows(_result(cid, "MISSED_CALLS"))) == ["L1"]

        monkeypatch.setattr(results, "fetch_attempts", real_fetch)  # the record turns up late
        _run_until(lambda: load_campaign(cid)["rows"][0]["status"] == "completed" and load_campaign(cid)["final_sent"])
        finals = [d for d in load_campaign(cid)["deliveries"] if d["kind"] == "final"]
        assert len(finals) == 2 and all(d["status"] == "sent" for d in finals)
        got = _csv_rows(_result(cid, "FINAL"))  # the corrected FINAL replaced the first one
        assert got["L1"]["Call Status"] == "Connected" and got["L1"]["Candidate Response"] == "Agreed to submit"
        assert not _result(cid, "MISSED_CALLS").exists()  # reached after all: no longer a missed call
    finally:
        object.__setattr__(settings, "public_base_url", original)


def test_unconfirmed_call_goes_to_review_and_is_never_dialled_again(monkeypatch):
    from ams.sarvam import SarvamError

    def no_answer_from_sarvam(*args, **kwargs):
        raise SarvamError("Sarvam didn't answer in 30 s", retryable=False, uncertain=True)
    monkeypatch.setattr(scheduler, "place_call", no_answer_from_sarvam)
    cid = _upload(_xlsx([["U1", "Uma", "9876500090", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]]), "unconfirmed.xlsx")
    scheduler.place_due_calls()
    scheduler.place_due_calls()
    row = load_campaign(cid)["rows"][0]
    assert row["status"] == "needs_review" and len(row["attempts"]) == 1 and row["attempt_count"] == 1
    assert "may have gone out" in row["note"]


def test_an_earlier_attempt_that_connected_wins_over_a_later_miss():
    from ams.campaigns import apply_result

    later = (datetime.now(LOCAL_TZ) + timedelta(hours=3)).strftime("%d-%m-%Y")
    cid = _upload(_xlsx([["E9", "Esha", "9876500095", "ABC", "PAN", "18-11-2026", later, "11:00 AM", "", ""]]),
                  "earlier.xlsx")
    with campaign_tx(cid) as camp:  # attempt 1 got no result in time; "Call again" placed attempt 2
        row = camp["rows"][0]
        row["attempts"] = [
            {"ref": "a1", "number": 1, "status": "no_result", "triggered_at": iso(utcnow() - timedelta(hours=1)), "sarvam_attempt_id": "S-1"},
            {"ref": "a2", "number": 2, "status": "awaiting_result", "triggered_at": iso(utcnow()), "sarvam_attempt_id": "S-2"}]
        row["status"] = "calling"
    meta = lambda ref: {"metadata": {"campaign_id": cid, "row_id": "R0001", "ref": ref}}  # noqa: E731
    assert apply_result({"attempt_id": "S-1", "status": "connected", "final_agent_variables": {"disposition_call_disposition": "agreed_to_submit"},
                         "webhook_config": meta("a1")}) == "processed"
    assert load_campaign(cid)["rows"][0]["status"] == "completed"
    apply_result({"attempt_id": "S-2", "status": "no_answer", "webhook_config": meta("a2")})
    row = load_campaign(cid)["rows"][0]
    assert row["status"] == "completed" and row["outcome"]["disposition_call_disposition"] == "agreed_to_submit"


def test_a_file_write_cut_off_by_a_restart_is_done_again():
    later = (datetime.now(LOCAL_TZ) + timedelta(hours=3)).strftime("%d-%m-%Y")
    cid = _upload(_xlsx([["S1", "Sana", "9876500096", "ABC", "PAN", "18-11-2026", later, "11:00 AM", "", ""]]),
                  "stuck.xlsx")
    scheduler.run_cycle()  # the "received" file
    with campaign_tx(cid) as camp:  # an update was being written when the server restarted
        camp["deliveries"].append({"key": "update-7", "kind": "update", "update_no": 7, "status": "sending",
                                   "failures": 0, "created_at": iso(utcnow() - timedelta(minutes=30)),
                                   "sending_at": iso(utcnow() - timedelta(minutes=30))})
    outbox.send_due_results()
    rec = next(d for d in load_campaign(cid)["deliveries"] if d["key"] == "update-7")
    assert rec["status"] == "sent" and rec["failures"] == 1 and "Update-7" in rec["file"]
    assert _result(cid, "RESULTS").exists()


def test_a_final_composed_before_call_again_does_not_close_the_campaign():
    from ams.campaigns import row_action

    cid = next(c for c in list_campaign_ids() if load_campaign(c)["final_sent"] and load_campaign(c).get("drop")
               and any(r["status"] == "failed" for r in load_campaign(c)["rows"]))
    with campaign_tx(cid) as camp:  # pretend the final was still being written...
        camp["final_sent"] = False
        camp["deliveries"].append({"key": "final-9", "kind": "final", "status": "sending", "failures": 0,
                                   "created_at": iso(utcnow()), "sending_at": stamp()})
        index = len(camp["deliveries"]) - 1
    row = next(r for r in load_campaign(cid)["rows"] if r["status"] == "failed")
    row_action(cid, row["row_id"], "call_again")  # ...when the operator pressed Call again
    outbox._send_one(cid, index)
    camp = load_campaign(cid)
    assert camp["deliveries"][index]["status"] == "sent" and not camp["final_sent"]  # a new final is still owed
    _run_until(lambda: load_campaign(cid)["final_sent"])


def test_call_again_after_final_writes_a_new_final():
    from ams.campaigns import row_action

    cid = next(c for c in list_campaign_ids() if load_campaign(c)["final_sent"] and load_campaign(c).get("drop")
               and any(r["status"] == "not_reachable" for r in load_campaign(c)["rows"]))
    row = next(r for r in load_campaign(cid)["rows"] if r["status"] == "not_reachable")
    finals_before = sum(1 for d in load_campaign(cid)["deliveries"] if d["kind"] == "final")
    assert "queued" in row_action(cid, row["row_id"], "call_again")
    assert not (campaign_dir(cid) / ".done").exists()
    _run_until(lambda: load_campaign(cid)["final_sent"])
    finals = [d for d in load_campaign(cid)["deliveries"] if d["kind"] == "final"]
    assert len(finals) == finals_before + 1 and finals[-1]["status"] == "sent"
