"""
Internal dashboard for the AMS team (clients use email, or the SFTP folder).

    streamlit run dashboard.py

Reads the same data/ folder as the backend, so run it on the same server.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AMS Calling Automation", layout="wide")

from ams import email_settings  # noqa: E402
from ams.campaigns import (ATTEMPT_LABELS, ROW_LABELS, WAITING, TERMINAL, campaign_action, change_recipient,  # noqa: E402
                           counts, delivers, next_call_time, row_action, slot_label)
from ams.config import BASE_DIR, settings  # noqa: E402
from ams.excel_out import build_results, format_duration, humanize, output_name  # noqa: E402
from ams.mailer import handle_file, held_emails, release_held_emails  # noqa: E402
from ams.zoho import ZohoError, client as zoho_client  # noqa: E402
from ams.passwords import verify_password  # noqa: E402
from ams.sarvam import fetch_recording, fetch_recording_url  # noqa: E402
from ams.store import campaign_dir, get_status, load_all_campaigns, load_campaign, read_log  # noqa: E402
from ams.timeutil import fmt_local, parse_iso, utcnow  # noqa: E402

CAMPAIGN_LABELS = {"scheduled": "Scheduled", "in_progress": "Calling", "completed": "Completed",
                   "rejected": "Rejected"}


# ---------------------------------------------------------------- login
def login() -> str:
    if st.session_state.get("user"):
        return st.session_state["user"]
    users = settings.dashboard_users()
    st.title("AMS Calling Automation")
    if not users:
        st.error("No dashboard accounts exist yet. Create config/users.json (README, step 5).")
        st.stop()
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            record = users.get(username.strip(), {})
            if verify_password(password, record.get("password", "")):
                st.session_state["user"] = username.strip()
                st.rerun()
            time.sleep(1)
            st.error("Wrong username or password.")
    st.stop()


# ---------------------------------------------------------------- overview
def _ago(iso_text) -> str:
    if not iso_text:
        return "never"
    secs = int((utcnow() - parse_iso(iso_text)).total_seconds())
    if secs < 90:
        return f"{secs} sec ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    return fmt_local(iso_text)


def system_panel() -> None:
    status = get_status()
    beat = status.get("scheduler_heartbeat")
    mail = status.get("mail_last_check") or {}
    hook = status.get("last_webhook") or {}
    alive = beat and (utcnow() - parse_iso(beat)).total_seconds() < settings.scheduler_poll_seconds * 3 + 30

    c = st.columns(3)
    c[0].metric("Scheduler", "Running" if alive else "Stopped", _ago(beat), delta_color="off")
    c[1].metric("Inbox last checked", _ago(mail.get("at")), "OK" if mail.get("ok") else (mail.get("error") or "")[:40]
                if mail else "", delta_color="off")
    c[2].metric("Last call result", _ago(hook.get("at")), hook.get("outcome", ""), delta_color="off")
    if not alive:
        st.error("The backend isn't running, so no calls or emails go out. Start: uvicorn ams.server:app")
    if mail and not mail.get("ok"):
        st.error(f"Zoho Mail error: {mail.get('error')}")
    poll = status.get("result_poll") or {}
    if poll and not poll.get("ok"):
        st.error(f"Couldn't fetch call results from Sarvam at {fmt_local(poll['at'])}: {poll.get('error')}. "
                 "It retries automatically every minute.")
    if paused := status.get("calls_paused"):
        st.error(f"Calls are paused since {fmt_local(paused['at'])}: {paused['reason']}. Waiting calls are kept, "
                 "not failed. Fix the Sarvam values in .env (python -m ams.sarvam_setup check) and restart the backend.")
    if settings.drop_dir:
        drop = status.get("drop_last_check") or {}
        if not drop:
            st.warning(f"The SFTP folder {settings.drop_dir} hasn't been checked yet (is the backend running?).")
        elif not drop.get("ok"):
            st.error(f"SFTP folder problem at {fmt_local(drop['at'])}: {drop.get('error')}. Files in the inbox wait "
                     "until it's fixed (deploy/setup_sftp.sh sets the folders up).")
        else:
            st.caption(f"SFTP folder {settings.drop_dir} checked {_ago(drop.get('at'))}"
                       + (f"; {drop['waiting']} file(s) still uploading" if drop.get("waiting") else ""))
    for label, missing in (("Calls", settings.missing_for_calling()), ("Email", settings.missing_for_mail())):
        if missing:
            st.warning(f"{label} can't work until these settings are filled in .env: {', '.join(missing)}"
                       + (". Scheduled calls wait until then." if label == "Calls" else ""))


def overview(campaigns: list[dict]) -> None:
    system_panel()
    totals = {"completed": 0, "scheduled": 0, "pending": 0}
    for camp in campaigns:
        c = counts(camp)
        totals["completed"] += c["connected"] + c["no_answer"] + c["failed"]
        totals["scheduled"] += c["waiting"]
        totals["pending"] += c["calling"]
    m = st.columns(4)
    m[0].metric("Total campaigns", len(campaigns))
    m[1].metric("Calls completed", totals["completed"])
    m[2].metric("Scheduled calls", totals["scheduled"])
    m[3].metric("Pending results", totals["pending"])

    if not campaigns:
        st.info("No campaigns yet. They appear here when a client emails a candidate file, "
                "or when you upload one in the New campaign tab.")
        return
    table = pd.DataFrame([{
        "Campaign": camp["campaign_id"],
        "Received": fmt_local(camp["created_at"]),
        "From": camp.get("reply_to") or ("SFTP folder" if camp.get("drop") else f"Upload by {camp.get('created_by')}"),
        "File": camp["file_name"],
        "Candidates": counts(camp)["total"],
        "Done": f"{counts(camp)['done']}/{counts(camp)['valid']}",
        "Status": CAMPAIGN_LABELS.get(camp["status"], camp["status"]),
        "Next call": fmt_local(next_call_time(camp)) if next_call_time(camp) else "",
        "Results sent": sum(1 for e in camp["emails"] if e["status"] == "sent"),
    } for camp in campaigns])
    st.caption("Select a campaign to see its calls.")
    event = st.dataframe(table, hide_index=True, on_select="rerun", selection_mode="single-row",
                         key=f"camp_table_{st.session_state.get('camp_round', 0)}")
    if event and event.selection.rows:
        st.session_state["campaign"] = table.iloc[event.selection.rows[0]]["Campaign"]


# ---------------------------------------------------------------- campaign detail
@st.cache_data(ttl=1800, max_entries=20, show_spinner="Loading recording...")
def _audio(interaction_id, app_id, recording_url):
    return fetch_recording_url(recording_url) if recording_url else fetch_recording(interaction_id, app_id)


def campaign_detail(cid: str, user: str) -> None:
    camp = load_campaign(cid)
    if not camp:
        st.warning("Campaign not found.")
        return
    c = counts(camp)
    top = st.columns([5, 1])
    top[0].subheader(f"{cid}: {camp['file_name']}")
    if top[1].button("Close"):
        st.session_state.pop("campaign", None)
        st.session_state["camp_round"] = st.session_state.get("camp_round", 0) + 1
        st.rerun()
    email = camp.get("email") or {}
    sender = camp["original_sender"] if "original_sender" in camp else camp.get("reply_to")
    st.caption(f"Received {fmt_local(camp['created_at'])}"
               + (f" through the SFTP folder (processed/{camp['drop'].get('processed_as')})" if camp.get("drop") else "")
               + (f" from {sender}, subject \"{email.get('subject', '')}\"" if sender else "")
               + (f". Results now go to {camp['reply_to']}" if camp.get("recipient_changes") else ""))
    if c["valid"]:
        st.progress(c["done"] / c["valid"], text=f"{c['done']} of {c['valid']} calls finished")
    m = st.columns(6)
    for col, (label, key) in zip(m, [("Connected", "connected"), ("No answer", "no_answer"), ("Failed", "failed"),
                                     ("Waiting", "waiting"), ("Under review", "review"), ("Invalid rows", "invalid")]):
        col.metric(label, c[key])

    if flash := st.session_state.pop("flash", None):
        st.success(flash)

    a = st.columns(4)
    a[0].download_button("Download current results", build_results(camp, final=camp["status"] == "completed"),
                         file_name=output_name(camp, "current"))
    a[1].download_button("Download original file", (campaign_dir(cid) / camp["input_file"]).read_bytes(),
                         file_name=camp["file_name"])
    if a[2].button("Write an update now" if camp.get("drop") else "Email an update now", disabled=not delivers(camp)):
        st.session_state["flash"] = campaign_action(cid, "send_update")
        st.rerun()
    if a[3].button("Cancel waiting calls", disabled=not c["waiting"]):
        st.session_state["flash"] = campaign_action(cid, "cancel_remaining")
        st.rerun()

    if camp.get("drop"):
        st.caption("Results are written to the client's SFTP folder: results/ (an in-progress file, then the FINAL "
                   "file) and errors/. Nothing is emailed for this campaign.")
    else:
        with st.expander(f"Results are emailed to: {camp.get('reply_to') or 'nobody (download only)'}. Change"):
            st.caption("Use this when the client's email id is replaced during a campaign. Updates, the final file "
                       "and resends go to the new address, still as replies in the original email thread.")
            with st.form(f"recipient_{cid}"):
                new_to = st.text_input("Email results to", value=camp.get("reply_to") or "")
                if st.form_submit_button("Save address"):
                    st.session_state["flash"] = change_recipient(cid, new_to, user)
                    st.rerun()

    rows = camp["rows"]
    table = pd.DataFrame([{
        "Row": r["excel_row"], "Candidate ID": r["candidate_id"], "Name": r["name"], "Phone": r["phone"],
        "Call time": slot_label(r["slot"]) if r["slot"] else "",
        "Status": ROW_LABELS.get(r["status"], r["status"]),
        "Next call": fmt_local(r["next_call_at"]) if r["status"] in WAITING else "",
        "Attempts": r["attempt_count"],
        "Response": humanize((r.get("outcome") or {}).get(next(iter(settings.result_field_map.values()), ""), "")),
        "Remarks": r.get("note", ""),
    } for r in rows])
    st.markdown("**Calls**")
    event = st.dataframe(table, hide_index=True, on_select="rerun", selection_mode="single-row",
                         key=f"rows_{cid}")
    if event and event.selection.rows:
        row_detail(camp, rows[event.selection.rows[0]])

    st.markdown("**Result files written to the SFTP folder**" if camp.get("drop") else "**Emails**")
    if camp["emails"]:
        st.dataframe(pd.DataFrame([{
            "Type": e["kind"].capitalize() + (f" {e.get('update_no')}" if e.get("update_no") else ""),
            "Status": e["status"], "Queued": fmt_local(e["created_at"]),
            "Sent": fmt_local(e.get("sent_at")) if e.get("sent_at") else "",
            "File": (e.get("file") or "").replace("outputs/", ""), "Error": e.get("error") or "",
        } for e in camp["emails"]]), hide_index=True)
        if st.button("Write the last result file again" if camp.get("drop") else "Resend last email"):
            st.session_state["flash"] = campaign_action(cid, "resend_last")
            st.rerun()
        for e in camp["emails"]:
            if e.get("file") and (campaign_dir(cid) / e["file"]).exists():
                st.download_button(f"Download {e['file'].replace('outputs/', '')}",
                                   (campaign_dir(cid) / e["file"]).read_bytes(),
                                   file_name=e["file"].replace("outputs/", ""), key=f"dl_{e['key']}")
    else:
        st.caption("No emails for this campaign" + ("" if camp.get("reply_to") else " (uploaded without an email address)."))


def row_detail(camp: dict, row: dict) -> None:
    cid = camp["campaign_id"]
    with st.container(border=True):
        st.markdown(f"**{row['name']}** ({row['candidate_id']}, {row['phone']}): "
                    f"{ROW_LABELS.get(row['status'], row['status'])}")
        st.caption(f"Pending documents: {row['documents']}")
        cols = st.columns(3)
        if row["status"] in WAITING:
            if cols[0].button("Call now", key=f"now_{row['row_id']}"):
                st.session_state["flash"] = row_action(cid, row["row_id"], "call_now")
                st.rerun()
            if cols[1].button("Cancel this call", key=f"cx_{row['row_id']}"):
                st.session_state["flash"] = row_action(cid, row["row_id"], "cancel")
                st.rerun()
        elif row["status"] in TERMINAL - {"invalid"}:
            if cols[0].button("Call again", key=f"again_{row['row_id']}"):
                st.session_state["flash"] = row_action(cid, row["row_id"], "call_again")
                st.rerun()
        if row.get("outcome"):
            st.table(pd.DataFrame([(k, str(v)) for k, v in row["outcome"].items()], columns=["Agent output", "Value"])
                     .set_index("Agent output"))
        for att in reversed(row["attempts"]):
            label = f"Attempt {att['number']}: {ATTEMPT_LABELS.get(att['status'], att['status'])}, {fmt_local(att['triggered_at'])}"
            if att.get("duration"):
                label += f", {format_duration(att['duration'])}"
            with st.expander(label, expanded=att is row["attempts"][-1]):
                if att.get("failure_reason"):
                    st.caption(att["failure_reason"])
                if att.get("interaction_id") or att.get("recording_url"):
                    key = f"play_{att['ref']}"
                    if st.session_state.get(key):
                        try:
                            audio = _audio(att.get("interaction_id"), att.get("app_id"), att.get("recording_url"))
                            if audio:
                                st.audio(audio, format="audio/wav")
                            else:
                                st.info("No recording available yet.")
                        except Exception as exc:
                            st.error(f"Couldn't load the recording: {exc}")
                    elif st.button("Play recording", key=f"btn_{key}"):
                        st.session_state[key] = True
                        st.rerun()
                for turn in att.get("transcript") or []:
                    agent = str(turn.get("role", "")).lower() in ("agent", "assistant", "bot")
                    with st.chat_message("assistant" if agent else "user"):
                        st.write(turn.get("indic_text") or turn.get("en_text") or turn.get("text") or "")
                        if turn.get("indic_text") and turn.get("en_text"):
                            st.caption(turn["en_text"])


# ---------------------------------------------------------------- new campaign / inbox
def new_campaign(user: str) -> None:
    st.subheader("New campaign from a file")
    st.write("For files that didn't come by email, or for testing. With an email address, the "
             "acknowledgement, updates and final file are emailed there; without one, download them here.")
    cols = st.columns(2)
    for col, (label, name) in zip(cols, (("Download the client template (CSV)", "AMS_Calling_Template.csv"),
                                         ("Download the client template (Excel, with instructions)",
                                          "AMS_Calling_Template.xlsx"))):
        tpl = BASE_DIR / "templates" / name
        if tpl.exists():
            col.download_button(label, tpl.read_bytes(), file_name=tpl.name, key=f"tpl_{name}")
    up = st.file_uploader("Candidate file", type=["xlsx", "csv", "xls"], key=f"up_{st.session_state.get('up_round', 0)}")
    to = st.text_input("Email results to (optional)")
    if st.button("Create campaign", type="primary", disabled=up is None):
        cid = handle_file(up.getvalue(), up.name, to.strip().lower() or None, None, source="dashboard", created_by=user)
        if cid:
            st.session_state.update(flash=f"Created {cid}.", campaign=cid,
                                    up_round=st.session_state.get("up_round", 0) + 1)
            st.rerun()
        st.error("The file couldn't be used (or it duplicates a recent file). See the Inbox log tab for the reason.")


SETTING_LABELS = {"mailbox": "AMS mailbox", "allowed_senders": "allowed client addresses", "cc": "copy of replies"}


def _show(value) -> str:
    if value is None:
        return "(.env value)"
    return ", ".join(value) if isinstance(value, list) else str(value) or "(none)"


def _check_mailbox(address: str) -> tuple[bool | None, str]:
    """(True/False, message) from Zoho, or (None, reason) when it can't be checked."""
    missing = [k for k in settings.missing_for_mail() if k != "ZOHO_FROM_ADDRESS"]
    if missing or not settings.mail_enabled:
        return None, "Zoho Mail isn't connected yet, so the address couldn't be checked against the mailbox."
    try:
        return zoho_client().check_mailbox(address)
    except ZohoError as exc:
        return None, f"Couldn't reach Zoho to check the address: {exc}"


def email_settings_tab(user: str) -> None:
    st.subheader("Email settings")
    st.write("Change these when your own address or the client's address is replaced. Changes apply within a "
             "minute without a restart, and override the values in .env. Campaigns already running keep "
             "replying to their own sender; change that on the campaign itself (Campaigns tab, select a campaign).")
    if flash := st.session_state.pop("settings_flash", None):
        st.success(flash)

    mailbox_now = email_settings.mailbox()
    senders_now = list(email_settings.allowed_senders())
    cc_now = email_settings.parse_addresses(email_settings.cc())[0]
    with st.form("email_settings"):
        mailbox = st.text_input("AMS mailbox: the fixed address clients send their files to", value=mailbox_now,
                                help="Replies are sent from this address. It must be the Zoho mailbox the refresh "
                                     "token was created for, or an alias of it.")
        st.caption(f"Set in {email_settings.source('mailbox')}. .env value: {settings.zoho_from_address or 'none'}")
        senders = st.text_area("Client addresses allowed to send files: one per line, @domain.com allows the whole domain",
                               value="\n".join(senders_now), height=130)
        st.caption(f"Set in {email_settings.source('allowed_senders')}. Emails from anyone else are not processed "
                   "and are listed below.")
        cc = st.text_input("Send a copy of every reply to (optional, separate several with commas)",
                           value=", ".join(cc_now))
        submitted = st.form_submit_button("Save", type="primary")

    if submitted:
        errors, notes, changes = [], [], {}
        new_mailbox = mailbox.strip().lower()
        if not new_mailbox and not mailbox_now:
            pass  # not set up yet; the other fields can still be saved
        elif not email_settings.is_email(new_mailbox):
            errors.append(f"AMS mailbox '{mailbox}' isn't a valid email address.")
        elif new_mailbox != mailbox_now.lower():
            ok, message = _check_mailbox(new_mailbox)
            if ok is False:
                errors.append(message)
            else:
                changes["mailbox"] = new_mailbox
                if ok is None:
                    notes.append(message)
        good, bad = email_settings.parse_addresses(senders, allow_domains=True)
        if bad:
            errors.append("These client addresses aren't valid: " + ", ".join(bad))
        elif not good:
            errors.append("Add at least one client address or @domain. An empty list would let anyone trigger calls.")
        elif good != senders_now:
            changes["allowed_senders"] = good
        cc_good, cc_bad = email_settings.parse_addresses(cc)
        if cc_bad:
            errors.append("These copy addresses aren't valid: " + ", ".join(cc_bad))
        elif cc_good != cc_now:
            changes["cc"] = cc_good

        if errors:
            for err in errors:
                st.error(err)
            st.caption("Nothing was saved.")
        elif not changes:
            st.info("Nothing changed.")
        else:
            email_settings.save(changes, user)
            msg = "Saved " + ", ".join(SETTING_LABELS[k] for k in changes) + "."
            if "allowed_senders" in changes and (n := release_held_emails()):
                msg += f" {n} waiting email(s) from newly allowed senders will be processed within a minute."
            st.session_state["settings_flash"] = " ".join([msg, *notes])
            st.rerun()

    cols = st.columns(2)
    if cols[0].button("Check the AMS mailbox in Zoho"):
        ok, message = _check_mailbox(mailbox_now)
        (st.success if ok else st.warning if ok is None else st.error)(message)
    if cols[1].button("Reset all to .env values", disabled=all(email_settings.source(k) == ".env"
                                                              for k in email_settings.KEYS)):
        email_settings.save({k: None for k in email_settings.KEYS}, user)
        n = release_held_emails()
        st.session_state["settings_flash"] = "Back to the .env values." + (
            f" {n} waiting email(s) from allowed senders will be processed within a minute." if n else "")
        st.rerun()

    st.markdown("**Emails waiting because the sender isn't allowed**")
    held = held_emails()
    if held:
        st.dataframe(pd.DataFrame([{"Seen": fmt_local(h["at"]), "From": h.get("from", ""),
                                    "Subject": h.get("subject", "")} for h in reversed(held)]), hide_index=True)
        st.caption("They stay unread in the inbox. Add the sender (or their @domain) above and they're processed "
                   "automatically. If someone opened one in Zoho, mark it unread again.")
    else:
        st.caption("None.")

    history = email_settings.history()
    if history:
        st.markdown("**Change history**")
        st.dataframe(pd.DataFrame([{
            "When": fmt_local(h["at"], "%d %b %Y %I:%M %p"), "By": h.get("by", ""),
            "Changed": "; ".join(f"{SETTING_LABELS.get(k, k)}: {_show(v)}" for k, v in h["changes"].items()),
            "Before": "; ".join(f"{SETTING_LABELS.get(k, k)}: {_show(v)}" for k, v in h.get("before", {}).items()),
        } for h in history]), hide_index=True)


def inbox_log() -> None:
    st.subheader("Inbox log")
    st.caption(f"Polling {email_settings.mailbox() or '(no AMS mailbox set)'}, folder "
               f"{settings.zoho_folder}, every {settings.mail_poll_seconds} sec. Allowed senders: "
               f"{', '.join(email_settings.allowed_senders()) or 'anyone'}.")
    if settings.drop_dir:
        st.caption(f"Also watching the SFTP folder {settings.drop_dir}/inbox every {settings.drop_poll_seconds} sec "
                   f"(files appear below as \"SFTP upload\"; dates read as "
                   f"{'MM/DD/YYYY' if settings.drop_date_order == 'mdy' else 'DD-MM-YYYY'}).")
    cutoff = get_status().get("mail_cutoff")
    st.caption(f"Only emails received after {fmt_local(cutoff)} (when the system went live) can start campaigns; "
               "older mail in the inbox is skipped. Forward an old email again to process it."
               if cutoff else "Go-live time is recorded when the backend first starts; mail already in the inbox "
                              "then is never processed.")
    entries = read_log("mail_log", 300)
    if not entries:
        st.info("Nothing received yet.")
        return
    st.dataframe(pd.DataFrame([{
        "When": fmt_local(e["at"], "%d %b %I:%M %p"), "From": e.get("from", ""), "File": e.get("file", ""),
        "Subject": e.get("subject", ""), "Result": e.get("result", ""), "Campaign": e.get("campaign", ""),
        "Reason": e.get("reason") or e.get("error") or "",
    } for e in entries]), hide_index=True)


# ---------------------------------------------------------------- page
user = login()
st.sidebar.write(f"Signed in as **{user}**")
if st.sidebar.button("Refresh"):
    st.rerun()
if st.sidebar.button("Sign out"):
    st.session_state.clear()
    st.rerun()

st.title("AMS Calling Automation")
tab_overview, tab_new, tab_log, tab_settings = st.tabs(["Campaigns", "New campaign", "Inbox log", "Email settings"])
with tab_overview:
    if not st.session_state.get("campaign") and st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    all_campaigns = load_all_campaigns()
    overview(all_campaigns)
    if st.session_state.get("campaign"):
        st.divider()
        campaign_detail(st.session_state["campaign"], user)
with tab_new:
    new_campaign(user)
with tab_log:
    inbox_log()
with tab_settings:
    email_settings_tab(user)
