# AMS Calling Automation (SFTP to Sarvam)

The client's system uploads candidate files to an SFTP folder on our server. The app schedules a Sarvam AI call for every row at the row's call date and time, fetches each call's result from Sarvam, and writes the client's own file back with result columns added, into the same SFTP folder. No email, no database (everything is kept as files in `data/`) and no public web address: the client connects over SFTP, and the app itself only makes outgoing connections to Sarvam.

```
client uploads CSV ─► SFTP inbox/ ─► campaign (one per file)
                                        │  each row called at its own date + time
                                        ▼
                         Sarvam Instant Outbound call ─► result fetched from Sarvam (every minute)
                                        │
     send/ + failed/ ◄──────────────────┘  RESULTS sheet at once → updated per finished time → FINAL
                                           + MISSED_CALLS sheet of everyone not reached
```

## What the client sees

They connect with SFTP (FileZilla, WinSCP or their own system) and see three folders. [CLIENT_SFTP_GUIDE.md](CLIENT_SFTP_GUIDE.md) is the guide to send them.

```
inbox/    they upload .csv or .xlsx files here (header row required); a file leaves the inbox once
          it's picked up (within about a minute; the original is kept in data/campaigns/<campaign>/)
send/     the output sheet: <file>_<campaign>_RESULTS.csv at once (the schedule), replaced after every
          finished call time, then <file>_<campaign>_FINAL.csv
failed/   <file>_<campaign>_MISSED_CALLS.csv: everyone not reached (no answer, busy, failed, under review,
          not called), kept up to date; and files that can't be used, each with a note (a.csv -> a.csv_ERROR.txt)
```

The output sheet (and the missed-calls sheet) has their columns exactly as sent, then: Call Status, Call Date & Time, Call Duration, Attempt Count, Candidate Response, Commitment & Timeline, Call Summary, Call Quality, Other Agent Outputs, Remarks, Recording Link and Transcript. Results are CSV by default, because the client's system reads them; `DROP_RESULT_FORMAT=xlsx` or `both` adds Excel (with a "Call Summary" sheet and clickable links).

The Recording Link is Sarvam's own signed link to that one recording. It opens with no login for 14 days, as long as "Public signed URLs" is on in Sarvam (Settings → Workspace → Data policy; only the org Owner can change it). Nobody ever sees your Sarvam account or API key. (With an optional public address, `PUBLIC_BASE_URL`, links go through this app instead and last 30 days.)

## Client file format

The client exports the fields of their own calling form; our template's names (`templates/AMS_Calling_Template.csv`) work too, as do the old Sarvam cohort headers. Columns are found by name, so their order doesn't matter, and a title row above the header is fine. The names are defined in `ams/sheet.py` (`FIELD_ALIASES`, `CLIENT_FORMAT`).

| Client's form field | Template column | Sent to the agent as |
|---|---|---|
| Person to Call | Candidate Name | `name` |
| Phone Number to Call | Phone Number | `phone_number`, 10 digits (and dialled); +91 and spaces are fine |
| Call on Behalf of | Hiring Company | `Name_of_Hiring_company` |
| Reason for Calling | Pending Documents | `pending_document_names`: the documents, separated by commas (the form has no documents field, so they go in Reason for Calling; a real Pending Documents column wins if present) |
| WhatsApp Message Date | WhatsApp Message Date | `whatsapp_message_date`, always as DD-MM-YYYY |
| Date to Call + Time to Call, or Date and Time To Call | Call Date + Call Time | when to call; `NOW` calls as soon as the file arrives |

All are required: a row missing one is listed in the results file with the reason (named as in the client's file, e.g. "Reason for Calling is empty") and isn't called, while the other rows go ahead. AI Call Agent Persona, AI Call Category and any other columns are kept and returned unchanged. A row with no date and time at all is called right away (`BLANK_TIME=now`) or rejected (`BLANK_TIME=reject`).

- **Every row is called at its own date and time**, whenever the file arrives: a file uploaded at 10:30 with a record for 11:15 the same day gets that call at 11:15. A time that has just passed (within 24 hours) is called right away; times outside the calling hours move to the next morning.
- **Dates are read the way each file writes them.** 25/11/2026 can only be day first and 11/25/2026 only month first, so one such date settles the whole file. Only when every date in a file could be read either way (05/11/2026) does `DROP_DATE_ORDER` decide (`mdy` for this client's form, which writes 11/25/2026). A file that uses both orders is refused as a whole, so no call goes out on a wrong day.
- **Nobody is called twice.** The same file uploaded again within `DUPLICATE_FILE_DAYS` goes to `failed/` with a note. A row whose phone number already has a call at the same date and time in another recent campaign (overlapping exports) is skipped, and its Remarks name that campaign. NOW rows have no time to compare, so they are always called.
- **Uploads are never read half-way.** A file is read only after it has stopped changing for `DROP_SETTLE_SECONDS` (60), and temporary upload names (`.filepart`, `.part`, `.tmp`, names starting with a dot) are skipped. Every file the app writes appears under its final name in one step.
- A file that can't be used at all (no header row, a missing column, not .csv/.xlsx, damaged) is moved to `failed/` with a note listing the problem and the expected columns.

After changing `CLIENT_FORMAT`, rebuild both templates with `python -m ams.make_templates`.

## Project layout

```
ams/server.py      FastAPI: health (+ optional webhook/recording links); runs the background work
ams/scheduler.py   places due calls, fetches results, flags lost ones, writes result files
ams/dropfolder.py  the client's SFTP folder: picks up files, writes result files and error notes
ams/outbox.py      decides and writes the RESULTS / FINAL files for each campaign
ams/results.py     fetches finished calls' results from Sarvam (no public address needed)
ams/campaigns.py   row states, retries, result handling, batch decisions
ams/sheet.py       reads and validates the client's file
ams/excel_out.py   builds the result file (CSV or Excel) from the client's original file
ams/sarvam.py      Sarvam: place a call, fetch results/transcripts, download recordings
ams/store.py       file storage with locks (data/)
dashboard.py       internal Streamlit dashboard
deploy/            server setup (setup_server.sh) and the client's SFTP folder (setup_sftp.sh)
tools/             mock Sarvam for safe local testing
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

The result columns read the agent's outputs: Candidate Response ← `disposition_call_disposition`, Commitment & Timeline ← `disposition_commitment_with_timeline`, Call Summary ← `summary`, Call Quality ← `call_conversation_quality`. Codes such as `agreed_to_submit` are shown as "Agreed to submit". If the agent's variables change, run `python -m ams.sarvam_setup check` (read-only); it lists the agent's variable names and says which mapping no longer matches. Override with `AGENT_VARIABLE_MAP` / `RESULT_FIELD_MAP` in `.env`. Any output not mapped still appears in "Other Agent Outputs".

### Step 3: No public address needed

Leave `PUBLIC_BASE_URL` empty. Every `RESULT_POLL_SECONDS` (default 60) the app asks Sarvam about the calls still waiting for a result, using the attempt ID Sarvam returned when each call was placed.

Optional: if the server ever gets a public HTTPS address, set `PUBLIC_BASE_URL` and `WEBHOOK_SECRET`. Sarvam then also posts each result straight to `PUBLIC_BASE_URL/webhooks/sarvam/<WEBHOOK_SECRET>` (fetching keeps running as a safety net), and recording links go through `PUBLIC_BASE_URL/rec/...` (signed with `LINK_SECRET`). Generate secrets with `python -c "import secrets;print(secrets.token_urlsafe(32))"`.

### Step 4: Dashboard accounts

```bash
python -m ams.passwords "a-strong-password"    # prints a hash
cp config/users.example.json config/users.json  # paste hashes in
```

### Step 5: Try it locally without real calls

Set `DROP_DIR` in `.env` to any folder, for example `DROP_DIR=D:/ams-drop`, plus these lines so calls go to the mock:

```
SARVAM_OUTBOUND_BASE=http://127.0.0.1:9000/api/outbounds
SARVAM_ANALYTICS_BASE=http://127.0.0.1:9000/api/analytics
CALL_WINDOW_START=00:00
CALL_WINDOW_END=23:59
```

Run each of these in its own terminal, then put a file into `D:/ams-drop/inbox` and watch `send/` and `failed/`:

```bash
MOCK_RESULT=digit uvicorn tools.mock_sarvam:app --port 9000
uvicorn ams.server:app --port 8000
streamlit run dashboard.py
```

With `MOCK_RESULT=digit`, a phone ending in 1 gives "no answer", 2 gives "busy", 3 gives "failed", and anything else connects. The automated tests do the same flow end to end: `python -m pytest -q`.

### Step 6: Deploy

On an Ubuntu/Debian server, `sudo bash deploy/setup_server.sh` installs and starts the app, then `sudo DROP_DIR=/backend/fileupload CLIENT_IPS=<client IP> bash deploy/setup_sftp.sh` creates the three folders in `DROP_DIR` (any path you like) and sets it in `.env`. It also creates the client's SFTP login, locked into that folder. The server administrator's guides, `sysadmin/DEPLOYMENT.md` and `sysadmin/SFTP_SETUP.md`, give the same setup as numbered manual steps with every choice explained; they are kept out of git and shared directly. In short, without the scripts:

The backend must run continuously on one server with a persistent `data/` folder. The dashboard runs on the same server because it reads the same files. From the project folder, with the venv active, run these two processes:

```bash
uvicorn ams.server:app --host 127.0.0.1 --port 8000 --workers 1
streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

Keep `--workers 1`: that one process runs the folder watcher, call scheduler, result fetcher and result writer together. On a server shared with other projects, any two free local ports work (the setup script takes `BACKEND_PORT` and `DASHBOARD_PORT`). Both processes must survive logouts and reboots (systemd on Linux). Reach the dashboard through an SSH tunnel (`ssh -L 8501:127.0.0.1:8501 <user>@<server>`), in addition to its own login.

### Step 7: First live check

Before giving the login to the client, upload a one-row file with your own number and `NOW` through SFTP. Check that the call arrives, that the file leaves `inbox/` and a RESULTS sheet appears in `send/`, and that the FINAL sheet appears with a playable recording link. A second row with a number that won't answer shows the MISSED_CALLS sheet in `failed/`. If Sarvam rejects the number format, change `SARVAM_PHONE_FORMAT`.

## How it behaves

**Batches.** With `BATCH_MODE=slot` (the default), rows sharing the same call date + time form one batch, and the results file is rewritten when every call in that batch is finished, retries included. When the last batch finishes, the FINAL file replaces it. `BATCH_MODE=size` rewrites it every `BATCH_SIZE` finished calls instead; `BATCH_MODE=final` writes only the received file and the final one.

**Calling hours.** Calls go out only between `CALL_WINDOW_START` and `CALL_WINDOW_END` on `CALL_DAYS` (India time). Times outside the window move to the next allowed slot; the results file's Remarks show the actual time.

**Retries.** "No answer" and "busy" are retried after `RETRY_GAP_MINUTES` until `MAX_ATTEMPTS` calls have been placed. "Failed" (for example a DND number) is not retried. If Sarvam's API is down, the call is retried a few minutes later and doesn't count as an attempt.

**Never double-calls.** Each row is claimed under a file lock before dialing, and each uploaded file is picked up once (restart-safe). If a call's result never arrives within `RESULT_TIMEOUT_MINUTES`, the row is marked "Result pending (under review)" instead of being redialed; check the Sarvam dashboard, then use Call again in the dashboard if needed. If Sarvam accepted a call request but never confirmed it (a timeout after sending), the row also goes to review instead of being dialled again, because the call may already have gone out.

**Call results.** Every `RESULT_POLL_SECONDS` the app asks Sarvam about the calls still waiting. After a candidate hangs up it waits up to 10 minutes for the agent's answers and the transcript to be saved, so they aren't stored blank. A call is only given up on ("under review") after a successful lookup past its deadline still doesn't find it: while Sarvam's results can't be read, nothing is given up and the dashboard shows the error. A result that turns up late (within 24 hours) still updates the row; if the FINAL file was already written, a corrected one replaces it. If Sarvam refuses the key for reading results, new calls pause, as they do when it refuses to place calls. A file write cut off by a restart is done again after 10 minutes.

## The dashboard

The Campaigns tab shows whether the scheduler, the SFTP folder and call results are working, totals across campaigns, and every campaign with progress. Select a campaign to see each candidate's status. From there you can play recordings and read transcripts, download the original or current results, write an update to the client's folder immediately, cancel waiting calls, call one candidate now or again, and write the last result file again. The New campaign tab creates a campaign from an uploaded file (for testing; its results are downloaded, not written to the client's folder). The File log shows every file picked up and what happened to it.

## Files

```
data/campaigns/AMS-20261120-001/campaign.json    every row, attempt, result and result file written
data/campaigns/AMS-20261120-001/input.csv        the client's original file
data/campaigns/AMS-20261120-001/outputs/         a copy of every result file written
data/state/file_log.jsonl                        every file picked up and what happened to it
data/state/drop_files.json                       files picked up from the SFTP folder (makes pickups restart-safe)
data/state/webhooks.jsonl                        every raw Sarvam result, fetched or posted (audit)
```

Back up `data/` regularly; it is the only state (the setup script adds a daily backup). Old campaign folders can be archived once their FINAL file is written (they contain a `.done` marker).

## Troubleshooting

The System panel at the top of the Campaigns tab shows what's wrong. "SFTP folder problem" means the folder is missing or the app has no access to it: run `deploy/setup_sftp.sh` again. When no calls go out, check for missing Sarvam settings or a stopped scheduler, and confirm the current time is inside the calling window. When results never arrive, the dashboard shows a red "Couldn't fetch call results" message with Sarvam's error; `python -m ams.sarvam_setup check` confirms the key and IDs. A call whose result never appears in Sarvam shows "Result pending (under review)" after `RESULT_TIMEOUT_MINUTES`; a result that turns up later (within 24 hours) still updates the row. If the client says a file disappeared, look in `failed/` for its note, or in the dashboard's File log.
