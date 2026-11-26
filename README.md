# AMS Email-to-Sarvam Calling Automation

Clients email a candidate spreadsheet to one fixed Zoho Mail address. The system schedules a Sarvam AI call for every row at the row's Call Date and Call Time, fetches each call's result from Sarvam, and replies in the same email thread with the client's own spreadsheet plus result columns. No database: everything is kept as files in `data/`. No public address either: the server only makes outgoing connections.

```
Client email + Excel ─► Zoho inbox ─► campaign (one per attachment)
                                        │  each row called at its Call Date + Call Time
                                        ▼
                         Sarvam Instant Outbound call ─► result fetched from Sarvam (every minute)
                                        │
           reply in same thread ◄───────┘  "received" → update per finished time slot → FINAL file
```

## What the client sees

They fill the predefined template, `templates/AMS_Calling_Template.csv` (or the Excel version `templates/AMS_Calling_Template.xlsx`, which has a "How to fill" sheet), and send it to the fixed address. Within a minute they get a reply confirming the campaign ID, how many calls were scheduled and from when to when, and which rows can't be called and why. Each time every call for one Call Date + Call Time finishes (including retries), they get an update reply with the whole spreadsheet as it stands. When the last call finishes they get the `_FINAL.xlsx` reply. Their columns, formatting and extra sheets come back unchanged, with these added on the right: Call Status, Call Date & Time, Call Duration, Attempt Count, Candidate Response, Commitment & Timeline, Call Summary, Call Quality, Other Agent Outputs, Remarks, Recording Link and Transcript. A "Call Summary" sheet is added at the end.

The Recording Link is Sarvam's own signed link to that one recording. It opens with no login for 14 days, as long as "Public signed URLs" is on in Sarvam (Settings → Workspace → Data policy; only the org Owner can change it). Nobody ever sees your Sarvam account or API key. (With an optional public address, `PUBLIC_BASE_URL`, links go through this app instead and last 30 days.)

The template headers are flexible. The old cohort headers (`phone_number`, `name`, `Name_of_Hiring_company`, `pending_document_names`, `whatsapp_message_date`) also work, a title row above the table is fine, and the time can be one column (`20-11-2026 10:30`) or Call Date + Call Time (`20-11-2026` and `10:30 AM`). `NOW` calls as soon as the email arrives. A row with no time is called right away (`BLANK_TIME=now`) or rejected (`BLANK_TIME=reject`).

## Client file format

Give the client `templates/AMS_Calling_Template.csv`: one header row, one candidate per row below it. It holds only what the agent needs plus when to call. The columns, in this order, are defined once in `ams/sheet.py` (`CLIENT_FORMAT`); after changing them, rebuild both templates with `python -m ams.make_templates`.

| Column | Sent to the agent as | What to write |
|---|---|---|
| Candidate Name | `name` | Name as the agent should say it |
| Phone Number | `phone_number` (and dialled) | 10-digit mobile, e.g. 9876543210, without +91 or 91 (Excel turns longer numbers into 9.19E+11) |
| Hiring Company | `Name_of_Hiring_company` | Company the candidate is joining |
| Pending Documents | `pending_document_names` | Documents still pending, separated by commas |
| WhatsApp Message Date | `whatsapp_message_date` | Date the WhatsApp reminder was sent, DD-MM-YYYY |
| Call Date | (scheduling only) | DD-MM-YYYY, e.g. 20-11-2026; may be empty with `NOW` |
| Call Time | (scheduling only) | India time, e.g. 10:30 AM or 16:00. `NOW` calls as soon as the email arrives |

All seven are required. A row missing a value is reported in the "received" reply and not called. Extra columns a client adds, such as their own ID, are kept and returned unchanged.

A file the system can't use gets a reply listing the problem, the attached CSV template and this table. Rows with problems (bad number, unreadable date) are reported and not called, while the rest go ahead. Results come back as an Excel file (`.xlsx`), even for CSV input, so the recording links are clickable and the summary sheet fits.

## Client SFTP folder (instead of email)

The client can also upload files over SFTP, and collect the results the same way, with no email at all. On the server, `sudo bash deploy/setup_sftp.sh` creates an SFTP-only login locked into its own folder (it can see nothing else on the server), sets `DROP_DIR` in `.env` and prints the login details to send to the client; see [DEVOPS_DEPLOYMENT.md](DEVOPS_DEPLOYMENT.md). Email keeps working alongside it.

```
DROP_DIR/inbox/       the client uploads .csv or .xlsx files here (header row required)
DROP_DIR/processed/   each file moves here once it's picked up (within about a minute)
DROP_DIR/results/     <file>_<campaign>_RESULTS.csv at once (the schedule, and rows that can't be called),
                      replaced after every finished call time, then <file>_<campaign>_FINAL.csv
DROP_DIR/errors/      files that can't be used, each with a note saying what to fix (a.csv -> a.csv_ERROR.txt)
```

- A file is read only after it has stopped changing for `DROP_SETTLE_SECONDS` (60), so a half-uploaded file is never used. Temporary upload names (`.filepart`, `.part`, `.tmp`, names starting with a dot) are skipped. Every file the app writes appears under its final name in one step.
- Results are CSV by default, because the client's system reads them (`DROP_RESULT_FORMAT=xlsx` or `both` for Excel). The client's own columns come first, unchanged, then the same result columns as by email. The Recording Link is a plain URL.
- The same safety rules as email apply: the same file uploaded again within `DUPLICATE_FILE_DAYS` isn't called again (it goes to `errors/` with a note), and a corrected FINAL replaces the old one if a late result arrives.
- **Every row is called at its own date and time**, whenever the file arrives: a file uploaded at 10:30 with a record for 11:15 the same day gets that call at 11:15. A time that has just passed (within 24 hours) is called right away; times outside the calling hours move to the next morning. The results file is updated as each call time finishes.
- **Dates are read the way each file writes them.** 25/11/2026 can only be day first and 11/25/2026 only month first, so one such date settles the whole file. Only when every date in a file could be read either way (05/11/2026) does `DROP_DATE_ORDER` decide (`mdy` for this client's form, which writes 11/25/2026). A file that uses both orders is refused as a whole, so no call goes out on a wrong day. The agent always gets the WhatsApp date as DD-MM-YYYY.
- **The client's calling form** is understood as it is: Person to Call → `name`, Call on Behalf of → `Name_of_Hiring_company`, Reason for Calling → `pending_document_names` (the form has no documents field, so the documents go there; a real Pending Documents column wins if present), WhatsApp Message Date → `whatsapp_message_date`, Phone Number to Call → `phone_number` (10 digits; +91 and spaces are fine), Date to Call + Time to Call (or Date and Time To Call) → when to call. AI Call Agent Persona, AI Call Category and any other columns are kept and returned unchanged. Row problems name the client's own column ("Reason for Calling is empty").
- **A record sent again is not called twice.** A row whose phone number already has a call at the same date and time in another campaign (within `DUPLICATE_FILE_DAYS`) is skipped, and its Remarks name that campaign. NOW rows have no time to compare, so they are always called.
- To try it on a PC, set `DROP_DIR` to any folder (e.g. `DROP_DIR=D:/ams-drop`), start the backend, and put a file in its `inbox`.

## Project layout

```
ams/server.py      FastAPI: health (+ optional webhook/recording links); runs the scheduler + inbox reader
ams/results.py     fetches finished calls' results from Sarvam (no public address needed)
ams/scheduler.py   places due calls, fetches results, flags lost ones, queues and sends emails
ams/mailer.py      Zoho inbox → campaigns; ack / update / final / rejection replies
ams/dropfolder.py  client SFTP folder → campaigns; result files written back there
ams/zoho.py        Zoho Mail API client (refresh-token OAuth)
ams/campaigns.py   row states, retries, result handling, batch decisions
ams/sheet.py       reads and validates the client's file
ams/excel_out.py   builds the result workbook from the client's original file
ams/sarvam.py      Sarvam: place a call, fetch results/transcripts, download recordings
ams/store.py       file storage with locks (data/)
ams/email_settings.py  mailbox / client / copy addresses the dashboard can change
dashboard.py       internal Streamlit dashboard
tools/             mock Zoho and mock Sarvam for safe local testing
tests/             end-to-end tests (python -m pytest -q)
```

## Setup

### Step 1: Install

Python 3.10 or newer, in a virtual environment in the project folder (`venv/`):

```bash
python -m venv venv                  # or: uv venv venv
venv\Scripts\activate                # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt      # or: uv pip install -r requirements.txt
cp .env.example .env
```

### Step 2: Sarvam values

`SARVAM_APP_ID` is already set to your agent, `Conversatio-3f6c465d-6251`. Fill in the rest of the Sarvam block in `.env`. The API key comes from Settings → API Key; create a new one rather than reusing any key that has been pasted into a chat or email. The org and workspace IDs are in the dashboard URL and Settings. `SARVAM_APP_VERSION` is the committed agent version you want calls to use. `SARVAM_CONNECTION_ID` and `SARVAM_AGENT_PHONE_NUMBER` come from Phone Numbers (the telephony connection and the number calls come from).

The sheet is passed to the agent as the variables `name`, `phone_number` (10 digits), `Name_of_Hiring_company`, `pending_document_names` and `whatsapp_message_date`, which are the headers of your cohort CSV. The result columns read the agent's outputs: Candidate Response ← `disposition_call_disposition`, Commitment & Timeline ← `disposition_commitment_with_timeline`, Call Summary ← `summary`, Call Quality ← `call_conversation_quality`. Codes such as `agreed_to_submit` are shown as "Agreed to submit". If the agent's variables change, run `python -m ams.sarvam_setup check` (read-only); it lists the agent's variable names and says which mapping no longer matches. Override with `AGENT_VARIABLE_MAP` / `RESULT_FIELD_MAP` in `.env`. Any output not mapped still appears in "Other Agent Outputs".

The `sarvam-mcp` server is not needed here. It lets AI assistants manage Sarvam; this app calls Sarvam's REST API directly.

### Step 3: Zoho Mail (Self Client, India data centre)

Go to https://api-console.zoho.in, signed in as the mailbox that clients will write to. Choose Self Client and create it, then copy the Client ID and Client Secret into `.env` along with `ZOHO_FROM_ADDRESS` (the fixed address). Next, open the Generate Code tab and enter this scope:

```
ZohoMail.messages.ALL,ZohoMail.accounts.READ,ZohoMail.folders.READ
```

Pick 10 minutes, generate the code, and within those 10 minutes run:

```bash
python -m ams.zoho_setup token <THE_CODE>     # prints ZOHO_REFRESH_TOKEN=... for .env
python -m ams.zoho_setup check                # read-only: shows account, inbox, unread mails
```

If the mailbox also receives other mail (for example calling sheets for another agent), create a folder in Zoho Mail, add a Zoho filter that moves the client's emails into it, and set `ZOHO_FOLDER` to the folder's exact name (in quotes if it has spaces, e.g. `ZOHO_FOLDER="AMS Pending Document"`). Only that folder is read. If the name doesn't match, mail reading stops with an error listing the folders that exist; it never falls back to the Inbox.

If you created a **Server-based** client instead (it has an Authorized Redirect URI), put that URI in `ZOHO_REDIRECT_URI`, run `python -m ams.zoho_setup url`, open the printed link signed in as the mailbox and accept. Then, within about 2 minutes, run `python -m ams.zoho_setup token "<the whole address the browser landed on>"`. The page itself may not load; the address still holds the code.

Set `ALLOWED_SENDERS` to the client's addresses or domain (for example `@clientcompany.com`). Mail from anyone else is logged and left unread for a person to look at. `ZOHO_FROM_ADDRESS`, `ALLOWED_SENDERS` and `REPLY_CC` are only starting values; change them later in the dashboard's Email settings tab.

Google Cloud credentials are not needed for this flow; Zoho handles all email.

### Step 4: No public address needed

Leave `PUBLIC_BASE_URL` empty. The server only makes outgoing connections: every `RESULT_POLL_SECONDS` (default 60) it asks Sarvam about the calls still waiting for a result, using the attempt ID Sarvam returned when each call was placed. The client's recording links are Sarvam's own signed links (see "What the client sees").

Optional: if the server ever gets a public HTTPS address, set `PUBLIC_BASE_URL` and `WEBHOOK_SECRET`. Sarvam then also posts each result straight to `PUBLIC_BASE_URL/webhooks/sarvam/<WEBHOOK_SECRET>` (fetching keeps running as a safety net), and recording links go through `PUBLIC_BASE_URL/rec/...` (signed with `LINK_SECRET`). Generate secrets with:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

### Step 5: Dashboard accounts

```bash
python -m ams.passwords "a-strong-password"    # prints a hash
cp config/users.example.json config/users.json  # paste hashes in
```

### Step 6: Try it locally without real calls or emails

Run each of these in its own terminal:

```bash
MOCK_RESULT=digit uvicorn tools.mock_sarvam:app --port 9000
uvicorn tools.mock_zoho:app --port 9100
uvicorn ams.server:app --port 8000
streamlit run dashboard.py
```

With these lines in `.env`:

```
SARVAM_OUTBOUND_BASE=http://127.0.0.1:9000/api/outbounds
SARVAM_ANALYTICS_BASE=http://127.0.0.1:9000/api/analytics
ZOHO_ACCOUNTS_URL=http://127.0.0.1:9100
ZOHO_MAIL_API_URL=http://127.0.0.1:9100
CALL_WINDOW_START=00:00
CALL_WINDOW_END=23:59
```

Then "send" a client email and read the replies:

```bash
curl -F file=@my_candidates.xlsx -F from=hr@clientcompany.com -F subject="Calling request" http://127.0.0.1:9100/_inject
curl http://127.0.0.1:9100/_outbox
```

With `MOCK_RESULT=digit`, a phone ending in 1 gives "no answer", 2 gives "busy", 3 gives "failed", and anything else connects. The automated tests do the same flow end to end:

```bash
python -m pytest -q
```

### Step 7: Deploy

On an Ubuntu/Debian server, `sudo bash deploy/setup_server.sh` does everything below; see [DEVOPS_DEPLOYMENT.md](DEVOPS_DEPLOYMENT.md). The manual version:

The backend must run continuously on one server with a persistent `data/` folder. The dashboard runs on the same server because it reads the same files. From the project folder, with the venv active, run these two processes:

```bash
uvicorn ams.server:app --host 127.0.0.1 --port 8000 --workers 1
streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

Keep `--workers 1`: that one process runs the inbox reader, call scheduler, result fetcher and email sender together. Both processes must survive logouts and reboots: use systemd on Linux, or on Windows a service manager such as NSSM or a Task Scheduler "at startup" task. Nothing needs to be reachable from the internet. Reach the dashboard through an SSH tunnel (`ssh -L 8501:127.0.0.1:8501 <user>@<server>`), in addition to its own login.

### Step 8: First live check

Before giving the address to the client, email yourself a one-row file with your own number and `NOW`. Check that the call arrives, that you get the "received" reply, and that the FINAL file comes back with a playable Listen link. If Sarvam rejects the number format, change `SARVAM_PHONE_FORMAT`.

## How it behaves

**Batches.** With `BATCH_MODE=slot` (the default), rows sharing the same Call Date + Call Time form one batch, and an update email goes out when every call in that batch is finished, retries included. If the last batch is also the end of the campaign, only the FINAL email is sent. `BATCH_MODE=size` sends an update every `BATCH_SIZE` finished calls instead; `BATCH_MODE=final` sends only the final file.

**Calling hours.** Calls go out only between `CALL_WINDOW_START` and `CALL_WINDOW_END` on `CALL_DAYS` (India time). Times outside the window move to the next allowed slot; the received reply shows the actual first and last call times.

**Retries.** "No answer" and "busy" are retried after `RETRY_GAP_MINUTES` until `MAX_ATTEMPTS` calls have been placed. "Failed" (for example a DND number) is not retried. If Sarvam's API is down, the call is retried a few minutes later and doesn't count as an attempt.

**Never double-calls.** Each row is claimed under a file lock before dialing. Each email is processed once (tracked by Zoho message ID). The same file from the same sender within `DUPLICATE_FILE_DAYS` gets an "already received" reply instead of new calls. If a call's result never arrives within `RESULT_TIMEOUT_MINUTES`, the row is marked "Result pending (under review)" instead of being redialed; check the Sarvam dashboard, then use Call again in the dashboard if needed. If Sarvam accepted a call request but never confirmed it (a timeout after sending), the row also goes to review instead of being dialled again, because the call may already have gone out.

**Call results.** Every `RESULT_POLL_SECONDS` the app asks Sarvam about the calls still waiting. After a candidate hangs up it waits up to 10 minutes for the agent's answers and the transcript to be saved, so they aren't stored blank. A call is only given up on ("under review") after a successful lookup past its deadline still doesn't find it: while Sarvam's results can't be read, nothing is given up and the dashboard shows the error. A result that turns up late (within 24 hours) still updates the row; if the final file was already sent, a corrected final follows in the same thread. If Sarvam refuses the key for reading results, new calls pause, as they do when it refuses to place calls. An email cut off mid-send (restart, crash) is retried after 10 minutes.

**Old mail is never processed.** The first time the backend starts, it records that moment as go-live (shown in the Inbox log tab). Emails that were already in the inbox before then are skipped and left untouched, even if their sender is allowed later, so old calling sheets can't trigger calls. To process an older email, forward it to the mailbox again.

**Bad files.** A file missing required columns, or one where no row is usable, gets a reply explaining what's wrong (with the template, or with the file showing the problem on each row). No calls are placed.

## The dashboard

The Campaigns tab shows whether the scheduler, inbox and call results are working, totals across campaigns, and every campaign with progress. Select a campaign to see each candidate's status. From there you can play recordings and read transcripts, download the original or current results, email an update immediately, cancel waiting calls, call one candidate now or again, and resend the last email. The New campaign tab creates a campaign from an uploaded file (with or without an email address for results). The Inbox log shows every email the system saw and what it did with it.

**Changing email addresses.** When your address or the client's is replaced, use the Email settings tab; no `.env` edit or restart is needed, and every change is logged with who made it.
- *AMS mailbox* is the fixed address clients send files to, and the From address of every reply. It is checked against Zoho before saving. It must be the mailbox the Zoho refresh token belongs to, or an alias of it; moving to a mailbox under a different Zoho login needs a new `ZOHO_REFRESH_TOKEN` (Step 3).
- *Client addresses allowed to send files* takes emails or `@domain`s, and can't be left empty. Emails from anyone else are listed on the tab and stay unread. Add the sender and they are processed within a minute, so the client doesn't need to resend.
- *Copy of every reply* is optional: your own address(es) receive a copy of every reply.

A campaign that is already running keeps replying to the address that sent it. To move its updates and final file to the client's new address, open the campaign and use **Results are emailed to … Change**. Replies stay in the original email thread.

## Files

```
data/campaigns/AMS-20261120-001/campaign.json    every row, attempt, result and email
data/campaigns/AMS-20261120-001/input.xlsx       the client's original attachment
data/campaigns/AMS-20261120-001/outputs/         every result file that was emailed
data/state/mail_log.jsonl                        inbox activity
data/state/webhooks.jsonl                        every raw Sarvam result, fetched or posted (audit)
data/state/processed_messages.json               emails already handled (and ones waiting for an allowed sender)
data/state/email_settings.json                   addresses changed in the dashboard (override .env)
data/state/settings_log.jsonl                    who changed which address, and when
data/state/drop_files.json                       files picked up from the SFTP folder (makes pickups restart-safe)
```

Back up `data/` regularly; it is the only state. Old campaign folders can be archived once their FINAL email is sent (they contain a `.done` marker).

## Troubleshooting

When the inbox shows an error, run `python -m ams.zoho_setup check`. "Token refresh failed" usually means the wrong `ZOHO_DC`, or a refresh token created for another account. When no calls go out, check the System panel at the top of the Campaigns tab for missing Sarvam settings or a stopped scheduler, and confirm the current time is inside the calling window. When results never arrive, the dashboard shows a red "Couldn't fetch call results" message with Sarvam's error; `python -m ams.sarvam_setup check` confirms the key and IDs. A call whose result never appears in Sarvam shows "Result pending (under review)" after `RESULT_TIMEOUT_MINUTES`; a result that turns up later (within 24 hours) still updates the row while the campaign is open.
# Bridge-between-Sarvam-and-Zoho
