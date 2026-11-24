"""
Whole flow against local mock Zoho + mock Sarvam (no real calls or emails):

    python -m pytest -q

client email -> campaign -> calls at scheduled times -> Sarvam webhooks ->
update email per finished time slot -> final email, all in the same thread.
"""
import io
import threading
import time
from datetime import datetime, timedelta

import pytest
import requests
import uvicorn
from openpyxl import Workbook, load_workbook

from ams import mailer, scheduler, server
from ams.campaigns import TERMINAL
from ams.store import campaign_dir, campaign_tx, list_campaign_ids, load_campaign
from ams.timeutil import LOCAL_TZ, iso, utcnow
from tools import mock_sarvam, mock_zoho

ZOHO = "http://127.0.0.1:19100"
# The template's 7 columns plus optional extras a client may add (their own ID, Language, notes)
TEST_HEADERS = ["Candidate ID", "Candidate Name", "Phone Number", "Hiring Company", "Pending Documents",
                "WhatsApp Message Date", "Call Date", "Call Time", "Language", "Additional Context"]


def _serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            return srv
        time.sleep(0.05)
    raise RuntimeError(f"server on {port} did not start")


@pytest.fixture(scope="module", autouse=True)
def servers():
    running = [_serve(mock_sarvam.app, 19000), _serve(mock_zoho.app, 19100), _serve(server.app, 18000)]
    yield
    for srv in running:
        srv.should_exit = True


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


def _inject(data: bytes, name: str, sender: str = "hr@client.test", subject: str = "AMS Calling Request"):
    r = requests.post(f"{ZOHO}/_inject", files={"file": (name, data)}, data={"from": sender, "subject": subject})
    return r.json()["messageId"]


def _outbox():
    return requests.get(f"{ZOHO}/_outbox").json()


def _attachment(mail) -> bytes:
    return requests.get(f"{ZOHO}/_upload", params={"path": mail["attachments"][0]["path"]}).content


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


def test_email_campaign_end_to_end():
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
    data = _xlsx(rows)
    mid = _inject(data, "Nov_batch.xlsx")
    _inject(data, "spam.xlsx", sender="stranger@elsewhere.test")

    stats = mailer.poll_inbox()
    assert stats["campaigns"] == 1 and stats["ignored"] == 1
    assert mailer.poll_inbox()["emails"] == 0  # already processed, nothing new
    cid = list_campaign_ids()[0]

    # acknowledgement goes back in the client's thread
    scheduler.run_cycle()
    ack = _outbox()[0]
    assert ack["in_reply_to"] == mid and ack["to"] == "hr@client.test" and ack["subject"].startswith("Re:")
    assert "Row 5" in ack["content"]  # the invalid row is reported (excel row 5 = C4)

    # first time slot (NOW) finishes -> one update email with results so far
    def slot_now_done():
        camp = load_campaign(cid)
        return all(r["status"] in TERMINAL for r in camp["rows"] if r["slot"] == "Immediate")
    _run_until(slot_now_done)
    scheduler.run_cycle()
    update = [m for m in _outbox() if m["subject"].startswith("Re:") and m["attachments"]][-1]
    assert "Update-1" in update["attachments"][0]["name"]

    header, got = _rows(load_workbook(io.BytesIO(_attachment(update)))["Candidates"])
    assert header[:10] == ["Candidate ID", "Candidate Name", "Phone Number", "Hiring Company", "Pending Documents",
                           "WhatsApp Message Date", "Call Date", "Call Time", "Language", "Additional Context"]
    assert got["C1"]["Call Status"] == "Connected" and got["C1"]["Candidate Response"] == "Agreed to submit"
    assert got["C1"]["Commitment & Timeline"] == "Will upload tomorrow" and got["C1"]["Call Quality"] == "Positive"
    assert "Next Follow-up Date" not in header and not got["C1"]["Other Agent Outputs"]  # nothing left unmapped
    assert got["C1"]["Additional Context"] == "keep me"
    assert got["C2"]["Call Status"] == "No Answer" and got["C2"]["Attempt Count"] == 2
    assert got["C3"]["Call Status"] == "Failed" and "NDNC" in got["C3"]["Remarks"]
    assert got["C4"]["Call Status"] == "Not called (invalid row)"
    assert got["C5"]["Call Status"] == "Scheduled"

    # the recording link in the Excel plays without any login
    wb = load_workbook(io.BytesIO(_attachment(update)))
    ws = wb["Candidates"]
    link_col = header.index("Recording Link") + 1
    link = ws.cell(2, link_col).hyperlink.target
    audio = requests.get(link)
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    assert requests.get(link[:-3] + "xyz").status_code == 404  # tampered link

    # make the later slot due now, then the final email arrives
    with campaign_tx(cid) as camp:
        for r in camp["rows"]:
            if r["status"] == "scheduled":
                r["next_call_at"] = iso(utcnow())
    _run_until(lambda: load_campaign(cid)["final_sent"])
    final = _outbox()[-1]
    assert final["attachments"][0]["name"].endswith("_FINAL.xlsx") and final["in_reply_to"] == mid
    _, got = _rows(load_workbook(io.BytesIO(_attachment(final)))["Candidates"])
    assert got["C5"]["Call Status"] == "Connected" and got["C6"]["Call Status"] == "Busy"
    assert (campaign_dir(cid) / ".done").exists()

    sent = len(_outbox())
    scheduler.run_cycle()
    assert len(_outbox()) == sent  # nothing is sent twice


def test_same_file_twice_is_not_called_again():
    data = _xlsx([["D1", "Asha", "9876500010", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]])
    _inject(data, "first.xlsx")
    mailer.poll_inbox()
    before = len(list_campaign_ids())
    _inject(data, "first-again.xlsx")
    mailer.poll_inbox()
    assert len(list_campaign_ids()) == before
    assert _outbox()[-1]["subject"].startswith("Re:") and "already received" in _outbox()[-1]["content"]


def test_unusable_file_gets_template_back():
    _inject(b"name,company\nX,Y\n", "wrong.csv")
    mailer.poll_inbox()
    reply = _outbox()[-1]
    assert "couldn't process" in reply["content"] and reply["attachments"][0]["name"] == "AMS_Calling_Template.csv"
    assert "Pending Documents" in reply["content"] and "DD-MM-YYYY" in reply["content"]  # the format rules


def test_predefined_csv_template_filled_in_by_the_client():
    from ams.sheet import TEMPLATE_HEADERS

    template = open("templates/AMS_Calling_Template.csv", encoding="utf-8-sig").read()
    assert template.strip().split(",") == TEMPLATE_HEADERS  # the file given to clients matches the code
    later = (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(hour=11, minute=0)
    rows = [  # Excel-saved CSV quirks included: dates with slashes, a date next to NOW, a number Excel broke
        f"Asha Verma,9876500051,ABC Ltd,\"PAN, Degree\",18-11-2026,{later:%d-%m-%Y},11:00 AM",
        "Ravi Kumar,9876500050,XYZ Pvt,Aadhaar,18/11/2026,20-11-2026,NOW",
        "Broken Number,9.19877E+11,XYZ Pvt,Aadhaar,18-11-2026,,NOW",
        "No Message Date,9876500052,XYZ Pvt,Aadhaar,,,NOW",
    ]
    data = (template.strip() + "\n" + "\n".join(rows) + "\n").encode("utf-8-sig")
    mid = _inject(data, "client_data.csv")
    mailer.poll_inbox()
    cid = next(c for c in list_campaign_ids() if (load_campaign(c).get("email") or {}).get("message_id") == mid)
    camp = load_campaign(cid)
    row = {r["name"]: r for r in camp["rows"]}
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


def test_email_ids_can_be_changed_from_the_dashboard():
    from ams import email_settings
    from ams.campaigns import campaign_action, change_recipient
    from ams.zoho import client

    try:
        # the client's new address isn't allowed yet: the email waits, unread
        data = _xlsx([["E1", "Neha", "9876500020", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]])
        mid = _inject(data, "new_address.xlsx", sender="new-hr@newclient.test")
        assert mailer.poll_inbox()["ignored"] == 1
        assert any(h["from"] == "new-hr@newclient.test" for h in mailer.held_emails())

        # the team allows it (plus a copy to themselves): the waiting email is processed, no resend needed
        email_settings.save({"allowed_senders": ["@client.test", "new-hr@newclient.test"], "cc": ["me@ams.test"]},
                            "tester")
        assert mailer.release_held_emails() == 1  # strangers stay held
        assert mailer.poll_inbox()["campaigns"] == 1
        cid = next(c for c in list_campaign_ids() if (load_campaign(c).get("email") or {}).get("message_id") == mid)
        scheduler.run_cycle()
        ack = next(m for m in _outbox() if m["in_reply_to"] == mid)
        assert ack["to"] == "new-hr@newclient.test" and ack["cc"] == "me@ams.test"

        # the client's address is replaced mid-campaign: results follow, still in the original thread
        assert "will now be emailed to" in change_recipient(cid, "replacement@newclient.test", "tester")
        assert "isn't a valid" in change_recipient(cid, "not-an-email", "tester")
        _run_until(lambda: load_campaign(cid)["final_sent"])
        final = [m for m in _outbox() if m["in_reply_to"] == mid][-1]
        assert final["to"] == "replacement@newclient.test" and final["attachments"][0]["name"].endswith("_FINAL.xlsx")

        # the same file from the original sender is still recognised as a duplicate
        _inject(data, "new_address_again.xlsx", sender="new-hr@newclient.test")
        mailer.poll_inbox()
        assert "already received" in _outbox()[-1]["content"]

        # the AMS mailbox is checked against Zoho and becomes the From address of replies
        zm = client()
        assert zm.check_mailbox("calling@ams.test")[0] and not zm.check_mailbox("other@ams.test")[0]
        email_settings.save({"mailbox": "alias@ams.test"}, "tester")
        campaign_action(cid, "resend_last")
        scheduler.run_cycle()
        assert _outbox()[-1]["from"] == "alias@ams.test" and _outbox()[-1]["to"] == "replacement@newclient.test"
        assert len(email_settings.history()) == 2
    finally:
        email_settings.save({k: None for k in email_settings.KEYS}, "tester")


def test_calls_wait_instead_of_failing_when_sarvam_is_not_usable(monkeypatch):
    from ams.sarvam import SarvamError
    from ams.store import get_status

    data = _xlsx([["P1", "Kiran", "9876500030", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]])
    mid = _inject(data, "paused.xlsx")
    mailer.poll_inbox()
    cid = next(c for c in list_campaign_ids() if (load_campaign(c).get("email") or {}).get("message_id") == mid)
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


def test_mail_already_in_the_inbox_at_go_live_is_never_processed():
    from ams.store import set_status

    set_status("mail_cutoff", iso(utcnow()))  # go-live now; mock emails carry an older receivedTime
    try:
        data = _xlsx([["O1", "Old", "9876500040", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]])
        _inject(data, "july_sheet.xlsx")  # from an allowed sender, so only the cutoff stops it
        before = len(list_campaign_ids())
        assert mailer.poll_inbox()["old"] == 1
        assert len(list_campaign_ids()) == before
        assert mailer.poll_inbox()["emails"] == 0  # remembered; not re-checked every minute
    finally:
        set_status("mail_cutoff", None)


def test_no_public_address_results_are_fetched_from_sarvam():
    """The production setup: no PUBLIC_BASE_URL, so Sarvam can't post results; the app fetches them."""
    from ams.config import settings
    from ams.store import get_status

    original = settings.public_base_url
    object.__setattr__(settings, "public_base_url", "")  # Settings is frozen; tests only
    try:
        data = _xlsx([["Q1", "Meera", "9876500070", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""],   # connects
                      ["Q2", "Kabir", "9876500072", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""],   # busy twice
                      ["Q3", "Isha", "9876500073", "ABC", "PAN", "18-11-2026", "", "NOW", "", ""]])   # fails
        mid = _inject(data, "no_public_address.xlsx")
        mailer.poll_inbox()
        cid = next(c for c in list_campaign_ids() if (load_campaign(c).get("email") or {}).get("message_id") == mid)
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

        final = [m for m in _outbox() if m["in_reply_to"] == mid][-1]
        wb = load_workbook(io.BytesIO(_attachment(final)))
        header, got = _rows(wb["Candidates"])
        assert got["Q1"]["Candidate Response"] == "Agreed to submit" and got["Q2"]["Call Status"] == "Busy"
        assert "Agent: Hello, am I speaking with Meera?" in got["Q1"]["Transcript"]
        link_cell = wb["Candidates"].cell(2, header.index("Recording Link") + 1)
        assert link_cell.hyperlink.target == meera["audio_url"]  # Sarvam's signed link, not this server
    finally:
        object.__setattr__(settings, "public_base_url", original)


def test_call_again_after_final_sends_a_new_final():
    from ams.campaigns import row_action
    cid = next(c for c in list_campaign_ids() if load_campaign(c)["final_sent"]
               and any(r["status"] == "not_reachable" for r in load_campaign(c)["rows"]))
    row = next(r for r in load_campaign(cid)["rows"] if r["status"] == "not_reachable")
    assert "queued" in row_action(cid, row["row_id"], "call_again")
    assert not (campaign_dir(cid) / ".done").exists()
    _run_until(lambda: load_campaign(cid)["final_sent"])
    finals = [e for e in load_campaign(cid)["emails"] if e["kind"] == "final"]
    assert len(finals) == 2 and all(e["status"] == "sent" for e in finals)
