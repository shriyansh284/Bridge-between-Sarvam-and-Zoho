"""
Internal dashboard for the AMS team (the client only uses the SFTP folder).

    streamlit run dashboard.py

Reads the same data/ folder as the backend, so run it on the same server.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AMS Calling Automation", layout="wide")

from ams.campaigns import (ATTEMPT_LABELS, ROW_LABELS, WAITING, TERMINAL, campaign_action, counts,  # noqa: E402
                           create_campaign, delivers, next_call_time, row_action, slot_label)
from ams.config import BASE_DIR, settings  # noqa: E402
from ams.excel_out import build_results, format_duration, humanize, output_name  # noqa: E402
from ams.passwords import verify_password  # noqa: E402
from ams.sarvam import fetch_recording, fetch_recording_url  # noqa: E402
from ams.sheet import SheetError  # noqa: E402
from ams.store import append_log, campaign_dir, get_status, load_all_campaigns, load_campaign, read_log  # noqa: E402
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
        st.error("No dashboard accounts exist yet. Create config/users.json (README, step 4).")
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
    drop = status.get("drop_last_check") or {}
    hook = status.get("last_webhook") or {}
    alive = beat and (utcnow() - parse_iso(beat)).total_seconds() < settings.scheduler_poll_seconds * 3 + 30

    c = st.columns(3)
    c[0].metric("Scheduler", "Running" if alive else "Stopped", _ago(beat), delta_color="off")
    c[1].metric("SFTP folder last checked", _ago(drop.get("at")),
                ("OK" + (f", {drop['waiting']} uploading" if drop.get("waiting") else "")) if drop.get("ok")
                else (drop.get("error") or "")[:40], delta_color="off")
    c[2].metric("Last call result", _ago(hook.get("at")), hook.get("outcome", ""), delta_color="off")
    if not alive:
        st.error("The backend isn't running, so no files are picked up and no calls go out. "
                 "Start: uvicorn ams.server:app")
    if not settings.drop_dir:
        st.warning("DROP_DIR isn't set in .env, so no client files are picked up. On the server, "
                   "deploy/setup_sftp.sh creates the SFTP folder and sets it.")
    elif drop and not drop.get("ok"):
        st.error(f"SFTP folder problem at {fmt_local(drop['at'])}: {drop.get('error')}. Files in the inbox wait "
                 "until it's fixed (deploy/setup_sftp.sh sets the folders up).")
    poll = status.get("result_poll") or {}
    if poll and not poll.get("ok"):
        st.error(f"Couldn't fetch call results from Sarvam at {fmt_local(poll['at'])}: {poll.get('error')}. "
                 "It retries automatically every minute.")
    if paused := status.get("calls_paused"):
        st.error(f"Calls are paused since {fmt_local(paused['at'])}: {paused['reason']}. Waiting calls are kept, "
                 "not failed. Fix the Sarvam values in .env (python -m ams.sarvam_setup check) and restart the backend.")
    if missing := settings.missing_for_calling():
        st.warning(f"Calls can't be placed until these settings are filled in .env: {', '.join(missing)}. "
                   "Scheduled calls wait until then.")


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
        st.info("No campaigns yet. They appear here when the client uploads a file to the SFTP folder, "
                "or when you upload one in the New campaign tab.")
        return
    table = pd.DataFrame([{
        "Campaign": camp["campaign_id"],
        "Received": fmt_local(camp["created_at"]),
        "From": "SFTP folder" if camp.get("drop") else f"Upload by {camp.get('created_by') or '?'}",
        "File": camp["file_name"],
        "Candidates": counts(camp)["total"],
        "Done": f"{counts(camp)['done']}/{counts(camp)['valid']}",
        "Status": CAMPAIGN_LABELS.get(camp["status"], camp["status"]),
        "Next call": fmt_local(next_call_time(camp)) if next_call_time(camp) else "",
        "Result files": sum(1 for d in camp["deliveries"] if d["status"] == "sent"),
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


def campaign_detail(cid: str) -> None:
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
    if camp.get("drop"):
        st.caption(f"Received {fmt_local(camp['created_at'])} through the SFTP folder. The output sheet goes to "
                   "its send/ folder, and the candidates not reached to failed/ (MISSED_CALLS).")
    else:
        st.caption(f"Uploaded in the dashboard by {camp.get('created_by') or '?'} on {fmt_local(camp['created_at'])}. "
                   "Download its results here.")
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
    if a[2].button("Write an update now", disabled=not delivers(camp),
                   help="Writes the current output sheet to the client's send/ folder"):
        st.session_state["flash"] = campaign_action(cid, "send_update")
        st.rerun()
    if a[3].button("Cancel waiting calls", disabled=not c["waiting"]):
        st.session_state["flash"] = campaign_action(cid, "cancel_remaining")
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

    if not camp.get("drop"):
        return
    st.markdown("**Result files written to the SFTP folder**")
    if not camp["deliveries"]:
        st.caption("None yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Type": d["kind"].capitalize() + (f" {d.get('update_no')}" if d.get("update_no") else ""),
        "Status": "written" if d["status"] == "sent" else d["status"], "Queued": fmt_local(d["created_at"]),
        "Written": fmt_local(d.get("sent_at")) if d.get("sent_at") else "",
        "File": (d.get("file") or "").replace("outputs/", ""), "Error": d.get("error") or "",
    } for d in camp["deliveries"]]), hide_index=True)
    if st.button("Write the last result file again"):
        st.session_state["flash"] = campaign_action(cid, "resend_last")
        st.rerun()
    for d in camp["deliveries"]:
        if d.get("file") and (campaign_dir(cid) / d["file"]).exists():
            st.download_button(f"Download {d['file'].replace('outputs/', '')}",
                               (campaign_dir(cid) / d["file"]).read_bytes(),
                               file_name=d["file"].replace("outputs/", ""), key=f"dl_{d['key']}")


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


# ---------------------------------------------------------------- new campaign / file log
def new_campaign(user: str) -> None:
    st.subheader("New campaign from a file")
    st.write("For testing, or a file the client sent another way. The calls are placed exactly as for an SFTP "
             "upload; download the results from the campaign (nothing is written to the client's folder).")
    cols = st.columns(2)
    for col, (label, name) in zip(cols, (("Download the template (CSV)", "AMS_Calling_Template.csv"),
                                         ("Download the template (Excel, with instructions)",
                                          "AMS_Calling_Template.xlsx"))):
        tpl = BASE_DIR / "templates" / name
        if tpl.exists():
            col.download_button(label, tpl.read_bytes(), file_name=tpl.name, key=f"tpl_{name}")
    up = st.file_uploader("Candidate file", type=["xlsx", "csv", "xls"], key=f"up_{st.session_state.get('up_round', 0)}")
    if st.button("Create campaign", type="primary", disabled=up is None):
        try:
            camp = create_campaign(up.getvalue(), up.name, source="dashboard", created_by=user)
        except SheetError as exc:
            append_log("file_log", {"from": f"Dashboard ({user})", "file": up.name, "result": "rejected",
                                    "reason": str(exc)})
            st.error(f"The file couldn't be used: {exc}")
            return
        append_log("file_log", {"from": f"Dashboard ({user})", "file": up.name, "result": "campaign",
                                "campaign": camp["campaign_id"], "rows": len(camp["rows"])})
        st.session_state.update(flash=f"Created {camp['campaign_id']}.", campaign=camp["campaign_id"],
                                up_round=st.session_state.get("up_round", 0) + 1)
        st.rerun()


def file_log() -> None:
    st.subheader("File log")
    if settings.drop_dir:
        st.caption(f"Watching {settings.drop_dir}/inbox every {settings.drop_poll_seconds} sec. A file is read once "
                   f"it has stopped changing for {settings.drop_settle_seconds} sec. Numeric dates are read the way "
                   f"each file writes them; when a file's dates could be read either way, as "
                   f"{'MM/DD/YYYY' if settings.drop_date_order == 'mdy' else 'DD-MM-YYYY'} (DROP_DATE_ORDER).")
    else:
        st.caption("DROP_DIR isn't set, so no SFTP folder is watched.")
    entries = read_log("file_log", 300)
    if not entries:
        st.info("No files received yet.")
        return
    st.dataframe(pd.DataFrame([{
        "When": fmt_local(e["at"], "%d %b %I:%M %p"), "From": e.get("from", ""), "File": e.get("file", ""),
        "Result": e.get("result", ""), "Campaign": e.get("campaign", ""),
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
tab_overview, tab_new, tab_log = st.tabs(["Campaigns", "New campaign", "File log"])
with tab_overview:
    if not st.session_state.get("campaign") and st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    all_campaigns = load_all_campaigns()
    overview(all_campaigns)
    if st.session_state.get("campaign"):
        st.divider()
        campaign_detail(st.session_state["campaign"])
with tab_new:
    new_campaign(user)
with tab_log:
    file_log()
