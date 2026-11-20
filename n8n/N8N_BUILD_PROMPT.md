# Build prompt: AMS email-to-Sarvam calling automation on n8n

> Paste everything below the line into your n8n AI workflow builder, or give it to the developer building the workflows.
> Values in `<ANGLE_BRACKETS>` are placeholders. Fill them in from the project's `.env`, and put secrets only into
> n8n credentials or environment variables, never into this prompt or into node parameters.

---

## Role and goal

You are building a production n8n automation (self-hosted n8n, latest stable version) that replaces an existing,
tested Python service. Build it as a set of n8n workflows that behave exactly as specified below. Where n8n
cannot do something natively, use a Code node (JavaScript). Do not invent API endpoints or fields: use exactly the
ones given here.

**What the automation does:** a client emails a spreadsheet of candidates to a fixed Zoho Mail address. Each
attachment becomes one **campaign**. Every row is called by a **Sarvam AI voice agent** at that row's Call Date +
Call Time (India time). Sarvam posts each call's result to an n8n webhook. The client gets replies **in the same
email thread**: a "received" acknowledgement, an **updated Excel** each time all calls for one scheduled time slot
are finished, and a **FINAL Excel** when every call in the campaign is done. The Excel is the client's own data
plus result columns. There is no external database: use n8n's built-in **Data Tables** for state (if Data Tables
aren't available on this n8n version, use JSON files through "Read/Write Files from Disk" on a persistent volume).

## Configuration (n8n environment variables / credentials)

| Name | Value | Where |
|---|---|---|
| `SARVAM_API_KEY` | `<SARVAM_VOICE_AGENTS_KEY>` (starts `sk_samvaad_`) | Header Auth credential, header `X-API-Key` |
| `SARVAM_ORG_ID` | `<SARVAM_ORG_ID>` | env |
| `SARVAM_WORKSPACE_ID` | `<SARVAM_WORKSPACE_ID>` | env |
| `SARVAM_APP_ID` | `<SARVAM_AGENT_ID>` | env |
| `SARVAM_APP_VERSION` | `13` (integer) | env |
| `SARVAM_CONNECTION_ID` | `<SARVAM_CONNECTION_ID>` | env |
| `SARVAM_AGENT_PHONE_NUMBER` | `<CALLING_NUMBER_E164>` | env |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REFRESH_TOKEN` | `<…>` | env (secrets) |
| `ZOHO_ACCOUNTS_URL` / `ZOHO_MAIL_URL` | `https://accounts.zoho.in` / `https://mail.zoho.in` | env |
| `ZOHO_MAILBOX` | `<AMS_MAILBOX_ADDRESS>` | Settings table (editable) |
| `ZOHO_FOLDER` | `AMS Pending Document` | env |
| `ALLOWED_SENDERS` | `<CLIENT_ADDRESS>,<TEST_CLIENT_ADDRESS>` (exact addresses) | Settings table (editable) |
| `REPLY_CC` | empty, or internal address(es) | Settings table (editable) |
| `WEBHOOK_SECRET`, `LINK_SECRET` | 32+ random characters each | env (secrets) |
| `N8N_PUBLIC_URL` | public HTTPS base URL of this n8n | env |
| Calling rules | window 09:00–21:00 IST, all 7 days; max 2 placed attempts; retry gap 60 min; result timeout 45 min; max 5 new calls per dispatcher run; recording links valid 30 days; duplicate-file window 7 days | env |

Set n8n's `GENERIC_TIMEZONE=Asia/Kolkata`. Store every timestamp as UTC ISO-8601; show IST to people in the
format `20 Nov 2026, 10:30 AM`.

## The client's file (predefined format)

The client fills a CSV (or the Excel equivalent) with exactly these 7 columns, in this order:

| Column | Sent to the agent as | Rule |
|---|---|---|
| Candidate Name | `name` | required |
| Phone Number | `phone_number` (10 digits) and dialled | required; normalise as below |
| Hiring Company | `Name_of_Hiring_company` | required |
| Pending Documents | `pending_document_names` | required, comma-separated text |
| WhatsApp Message Date | `whatsapp_message_date` | required; output as `DD-MM-YYYY` |
| Call Date | scheduling only | `DD-MM-YYYY`; may be empty when Call Time is `NOW` |
| Call Time | scheduling only | `10:30 AM`, `16:00`, or `NOW` |

Reading rules (implement in a Code node after "Extract from File"; configure extraction to return raw cell text so
phone numbers and dates arrive as typed):
- Accept `.xlsx`, `.xlsm`, `.csv` (UTF-8 with or without BOM, else Windows-1252). Ignore other attachments.
- Find the header row within the first 15 rows: a row with a phone column and at least one other known column.
  Match headers loosely: lower-case, non-alphanumerics become `_`, then match aliases, e.g. phone: `phone_number,
  phone, mobile, mobile_number, contact_number`; name: `candidate_name, name, full_name`; company: `hiring_company,
  name_of_hiring_company, company`; documents: `pending_documents, pending_document_names, documents`;
  message date: `whatsapp_message_date, message_date`; call date: `call_date, scheduled_call_date`; call time:
  `call_time, scheduled_call_time`; optional `candidate_id`, `language`. Extra columns are kept and returned unchanged.
- Missing a required column, or no data rows → reject the whole file (see "unusable file" email). Skip blank rows.
- **Phone:** 10 digits starting 6–9 → `+91XXXXXXXXXX`; `91` + 10 digits, `0` + 10 digits, `091` + 10 digits are
  also Indian; scientific notation like `9.19877E+11` is invalid (Excel destroyed it); anything else invalid.
  Send `+91…` to Sarvam; send the 10-digit form as the `phone_number` agent variable.
- **Dates:** `DD-MM-YYYY`, `DD/MM/YYYY`, `DD.MM.YYYY`, `DD-MM-YY`, `YYYY-MM-DD`, `DD-Mon-YYYY`, Excel date cells.
  **Times:** `HH:MM`, `H:MM AM/PM`, `10.30`, `4 PM`, Excel time fractions. A single combined date-time column is also allowed.
- `NOW`/`ASAP`/`IMMEDIATE` in Call Time (or Call Date) → call immediately, even if a date is filled. Empty time → call immediately.
- A time more than 24 h in the past → row error "Call time … is in the past". Less than 24 h in the past → call now (warning).
- A time outside the calling window moves to the next allowed moment (09:00 next day, etc.); say so in the row's note.
- Same phone + same slot twice in one file → second row error.
- Each failing row gets a readable error (e.g. "Phone number '12345' is not a valid mobile number",
  "WhatsApp Message Date is empty") and is **not called**; the rest of the file still runs.
- The **slot** of a row is its local `YYYY-MM-DD HH:MM` (or `Immediate`). Slots drive the batch emails.

## Data Tables (all IDs are strings; JSON stored as text)

- `ams_settings`: `key`, `value`. Keys: `mailbox`, `allowed_senders` (comma list), `cc`, `mail_cutoff` (ISO),
  `calls_paused_until` (ISO), `lock_<workflow>` (ISO expiry).
- `ams_campaigns`: `campaign_id` (`AMS-YYYYMMDD-NNN`, IST date, sequence per day), `created_at`, `source_message_id`,
  `folder_id`, `attachment_id`, `subject`, `original_sender`, `reply_to`, `file_name`, `file_sha256`, `headers` (JSON
  list of the client's original column names), `status` (`scheduled|in_progress|completed|rejected`),
  `slots_sent` (JSON list), `final_sent` (bool).
- `ams_rows`: `campaign_id`, `row_id` (`R0001`…), `excel_row`, `original` (JSON object: the client's original cells by
  header), `name`, `phone_e164`, `agent_variables` (JSON), `app_overrides` (JSON or empty), `slot`, `next_call_at`,
  `status`, `note`, `attempt_count`, `trigger_errors`, `outcome` (JSON of the connected call's agent outputs).
- `ams_attempts`: `campaign_id`, `row_id`, `ref` (random UUID), `number`, `status`, `triggered_at`, `sarvam_attempt_id`,
  `result_at`, `duration`, `interaction_id`, `failure_reason`, `variables` (JSON), `transcript` (JSON).
- `ams_emails`: `campaign_id`, `key` (`ack`, `update-1`, `final-1`…; unique per campaign), `kind`
  (`ack|rejected|update|final`), `update_no`, `slots` (JSON), `status` (`pending|sending|sent|failed`), `failures`,
  `created_at`, `sent_at`, `error`.
- `ams_processed_messages`: `message_id`, `result` (`campaigns|ignored_sender|no_spreadsheet|before_go_live|duplicate|rejected`),
  `from`, `subject`, `at`.
- `ams_log`: `at`, `type` (`mail|webhook|error`), `data` (JSON), for audit.

Row status flow: `scheduled → calling → completed` (answered); `calling → retry_scheduled → calling …` (no answer /
busy, until 2 placed attempts) → `not_reachable`; `calling → failed` (provider failure, not retried);
`calling → needs_review` (no result in time; never auto-redialled); plus `invalid` (never called) and `cancelled`.
Terminal: `completed, not_reachable, failed, needs_review, cancelled, invalid`.

## Workflow 1: Inbox intake (Schedule Trigger, every 1 minute)

1. Take a lock (`lock_intake` in `ams_settings`, 5-minute expiry); exit if another run holds it.
2. Get a Zoho access token: `POST {ZOHO_ACCOUNTS_URL}/oauth/v2/token?refresh_token=…&client_id=…&client_secret=…&grant_type=refresh_token`
   → `access_token` (valid `expires_in` seconds; cache it in static data until 60 s before expiry). Every Zoho Mail call
   sends header `Authorization: Zoho-oauthtoken <access_token>`.
3. `GET {ZOHO_MAIL_URL}/api/accounts` → pick the account whose addresses include `mailbox` (exact, case-insensitive).
4. `GET /api/accounts/{accountId}/folders` → the folder whose `folderName` equals `ZOHO_FOLDER` exactly.
   **If it isn't found, stop with an error listing the folder names. Never fall back to the Inbox**: the Inbox holds
   calling sheets for a different agent that must never be processed.
5. `GET /api/accounts/{accountId}/messages/view?folderId={folderId}&status=unread&attachedMails=true&limit=100&sortorder=true`.
6. For each message (skip any `messageId` already in `ams_processed_messages`):
   - **Go-live cutoff:** `receivedTime` (epoch ms) earlier than `mail_cutoff` → record `before_go_live`, leave unread, stop.
     (`mail_cutoff` is set once, the first time the system starts.)
   - **Sender check:** extract the email from `fromAddress`; allowed only if it exactly matches an entry in
     `allowed_senders` (an entry starting with `@` allows a domain, but **never configure the client's whole domain
     `@<CLIENT_DOMAIN>`**: another agent's calling sheets come from that same domain). Not allowed → record `ignored_sender` with subject, **leave unread**, stop.
   - `GET /api/accounts/{a}/folders/{folderId}/messages/{m}/attachmentinfo` → keep attachments ending
     `.xlsx/.xlsm/.csv`. None → record `no_spreadsheet`, stop.
   - For each spreadsheet: skip if a campaign already exists for (message id, attachment id); download
     `GET /api/accounts/{a}/folders/{f}/messages/{m}/attachments/{attachmentId}` (binary);
     SHA-256 it; **duplicate** if a non-rejected campaign has the same hash and the same original sender within 7 days
     → send the "already received" reply, no calls. Otherwise parse + validate and create the campaign, its rows, and
     an `ack` email (or a `rejected` email if no row is valid). Unusable file → send the "unusable file" reply.
   - Record the message as processed, then `PUT /api/accounts/{a}/updatemessage` with `{"mode":"markAsRead","messageId":[<int>]}`.

## Workflow 2: Call dispatcher (Schedule Trigger, every 20–60 seconds)

1. Lock (`lock_dispatch`). Exit if `calls_paused_until` is in the future, or any Sarvam setting is missing (rows just wait).
2. **Stale results:** attempts `awaiting_result` older than 45 min (or `triggering` older than 10 min) → attempt
   `no_result`; if it is the row's latest attempt and the row is `calling` → row `needs_review`, note
   "No call result received. Check the Sarvam dashboard before retrying." Never redial automatically.
3. Due rows: status `scheduled|retry_scheduled` and `next_call_at <= now`, oldest first, max 5. If now is outside the
   calling window, move them to the next allowed time and stop.
4. For each due row: **claim** it first (update status to `calling` only if it is still waiting; dial only if the
   claim succeeded) and insert an attempt `{ref, number, status:"triggering", triggered_at}`. Then:
   ```
   POST https://apps.sarvam.ai/api/outbounds/v1/orgs/{SARVAM_ORG_ID}/workspaces/{SARVAM_WORKSPACE_ID}/outbounds
   X-API-Key: <key>
   {
     "app_config": {
       "app_id": "<SARVAM_APP_ID>", "app_version": 13,
       "connection_config": {"connection_id": "<SARVAM_CONNECTION_ID>", "agent_phone_number": "<CALLING_NUMBER_E164>"},
       "agent_variables": {"name": "...", "phone_number": "9876543210", "Name_of_Hiring_company": "...",
                           "pending_document_names": "...", "whatsapp_message_date": "18-11-2026"}
     },
     "user_config": {"user_phone_number": "+919876543210"},
     "webhook_config": {"url": "<N8N_PUBLIC_URL>/webhook/ams/sarvam/<WEBHOOK_SECRET>",
                        "metadata": {"campaign_id": "...", "row_id": "...", "ref": "..."}}
   }
   ```
   (Add `"app_overrides": {"initial_language_name": "Hindi"}` only when the row has a supported language.)
   - 2xx `{"attempt_id": "..."}` → store it; attempt `awaiting_result` (unless the result already arrived);
     `attempt_count` = number of placed attempts.
   - **401/403** → the key/IDs are wrong: delete this attempt, put the row back to waiting with note "Not called yet:
     Sarvam rejected the API key or workspace IDs", set `calls_paused_until` = now + 5 min, stop the run.
     Never mark candidates failed for an account problem.
   - Network error, 408, 409, 425, 429, 5xx → attempt `trigger_error`; row `retry_scheduled` in 5 min; after 3 such
     errors in a row → `needs_review`.
   - Other 4xx → row `failed` with the error text.

## Workflow 3: Sarvam result webhook (Webhook node, POST `ams/sarvam/<WEBHOOK_SECRET>`)

Respond `200 {"ok":true}` immediately, then process. A wrong path segment must get 404 (the secret is in the path).
Payload fields: `attempt_id`, `status` (`connected|no_answer|busy|failed`), `duration` (seconds or null),
`interaction_id`, `failure_reason`, `final_agent_variables` (object or null), `interaction_transcript`
(`[{role:"agent"|"user", en_text}]` or null), `webhook_config.metadata` (our `campaign_id,row_id,ref`).
1. Log the raw payload. Find the attempt by metadata; fall back to `sarvam_attempt_id`. Unknown → log "unmatched".
2. If the attempt already has a result → ignore (duplicate delivery). Save the result on the attempt.
3. If it isn't the row's latest attempt, or the row was cancelled → stop (keep the data only).
4. `connected` → row `completed`, `outcome` = `final_agent_variables`. `no_answer`/`busy` → if placed attempts < 2:
   row `retry_scheduled` at next allowed time ≥ now + 60 min, note "No Answer; retrying at …"; else `not_reachable`,
   note "No Answer after 2 attempt(s)". `failed` or unknown → row `failed`, note = `failure_reason`
   (e.g. "exotel: Phone number is registered under TRAI NDNC").
5. Update the campaign status (`completed` when every valid row is terminal).

## Workflow 4: Email dispatcher (Schedule Trigger, every 1 minute)

1. **Decide** (per campaign with a `reply_to`, not rejected, `final_sent` false, no final already queued):
   - all valid rows terminal → queue `final-N`;
   - else for each slot not in `slots_sent` whose rows are all terminal → add to `slots_sent`, queue `update-N`
     listing those slots. (If the last slot also finishes the campaign, only the final is sent.)
2. **Send** every `pending` (or `failed` with failures < 5) email; mark `sending` first so nothing goes twice.
   - Build the attachment (see "Result Excel"), upload it:
     `POST /api/accounts/{a}/messages/attachments?uploadType=multipart&isInline=false` (multipart field `attach`)
     → `{storeName, attachmentPath, attachmentName}`.
   - Reply in the original thread: `POST /api/accounts/{a}/messages/{source_message_id}` JSON
     `{"action":"reply","fromAddress":"<mailbox>","toAddress":"<reply_to>","ccAddress":"<cc if any>",
     "subject":"Re: <original subject>","content":"<html>","mailFormat":"html","attachments":[{…upload result…}]}`.
   - Success → `sent` (+ `final_sent` = true for finals). Failure → `failed`, failures + 1, keep the error.

Email content (HTML, Arial 14px, signature "Regards,<br>AMS Calling Team"):
- **ack**: "We've received <file> and created campaign <id>." + table: Candidates in file, Scheduled for calling,
  First call, Last call, Rows that can't be called; then the list "Row N (name): errors"; then "You'll receive an
  updated result file as each scheduled call time finishes, and a final file once every call in this campaign is complete."
- **update**: "Update N: calls scheduled for 20 Nov 2026, 10:30 AM are finished. The attached file includes every result
  available so far; more updates will follow." + summary table (Calls finished x of y, Connected, No answer / busy,
  Failed, [Under review], [Cancelled], [Invalid rows], Still scheduled, Next call). Attachment `AMS_Calling_Results_<id>_Update-N.xlsx`.
- **final**: "All calls for <file> are finished. The attached file is the final result for campaign <id>." + summary.
  Attachment `AMS_Calling_Results_<id>_FINAL.xlsx`.
- **rejected** (no valid rows): list each row's errors; attach the result file showing the problem per row; add the format table.
- **unusable file**: "We couldn't process <file>: <reason>. No calls were placed. Please fill the attached template, one
  candidate per row, and send it again." + the format table (Column / Required / What to write / Example);
  attach the CSV template (header row only).
- **already received**: "This file was already received as campaign <id> and is being processed. No calls were placed again."

## Result Excel

Original columns first (exact original headers and values, original row order, invalid rows included), then:
`Call Status, Call Date & Time, Call Duration, Attempt Count, Candidate Response, Commitment & Timeline, Call Summary,
Call Quality, Other Agent Outputs, Remarks, Recording Link, Transcript`.
- Call Status: Scheduled / Retry scheduled / Calling / Connected / No Answer / Busy (last attempt of an unreachable row) /
  Failed / Not called (invalid row) / Cancelled / Result pending (under review).
- Call Date & Time: IST time of the connected attempt, else the last attempt. Duration: `N sec` or `M min SS sec`.
- From the connected attempt's agent outputs: Candidate Response ← `disposition_call_disposition`, Commitment & Timeline ←
  `disposition_commitment_with_timeline`, Call Summary ← `summary`, Call Quality ← `call_conversation_quality`. Codes made
  of lower-case letters and underscores become readable (`agreed_to_submit` → `Agreed to submit`). Other Agent Outputs:
  any remaining outputs as `key: value; …`, excluding the 5 input variables.
- Remarks: the row's note; waiting rows say "Call scheduled for <IST time>".
- Recording Link (connected calls only): `<N8N_PUBLIC_URL>/webhook/ams/rec/<token>`, see Workflow 5.
- Transcript: lines `Agent: …` / `Candidate: …` from `en_text`, capped at 32,000 characters.
Use "Convert to File" (XLSX). Optional, if the Code node may load `xlsx` (SheetJS) via `NODE_FUNCTION_ALLOW_EXTERNAL`:
make the link a clickable "Listen" hyperlink and add a second sheet "Call Summary" with the campaign counts.

## Workflow 5: Recording link (Webhook node, GET `ams/rec/:token`)

Token = `base64url("campaign_id|row_id|ref|expiry_epoch")` + `.` + `base64url(HMAC-SHA256(LINK_SECRET, body))[first 18 bytes]`,
expiry = created + 30 days. On request: verify signature (constant-time) and expiry, else 404 "This recording link is
invalid or has expired." Find the attempt, then
`GET https://apps.sarvam.ai/api/analytics/v1/{ORG}/{WORKSPACE}/{APP_ID}/recordings/{url-encoded interaction_id}` with
`X-API-Key` → respond with the binary as `audio/wav` (`Content-Disposition: inline`). Sarvam 404 → 404 "No recording is
available for this call yet." The Sarvam key is never exposed to the person opening the link.

## Workflow 6: Team actions (n8n Forms, internal only)

Forms for: create a campaign from an uploaded file (optional results email); change `allowed_senders`, `mailbox`, `cc`
(validate addresses; refuse an empty sender list; after adding a sender, delete that sender's `ignored_sender` records
so their still-unread emails are processed on the next intake run); change a campaign's results address (`reply_to`,
keep `original_sender` for duplicate checks); call a row now / cancel / call again (call again on a finished campaign
re-opens it so a new FINAL is sent); resend the last email. Show state through the Data Tables views.

## Hard rules

- One attachment = one campaign; results never mix between campaigns.
- Never call the same row twice at once (claim before dialling); never process an email twice (processed list +
  campaign lookup by message id/attachment id); never send an email twice (status `sending` before sending, unique keys).
- Read only `ZOHO_FOLDER`; never the Inbox. Never process mail older than `mail_cutoff`. Leave unallowed mail unread.
- No secrets in workflow JSON, node parameters or logs. Sarvam key only in the Header Auth credential.
- Only one instance of each scheduled workflow runs at a time (locks), and only one n8n main instance runs the schedules.

## Deliverables

1. Data Table definitions (name, columns, types).
2. Importable workflow JSON for workflows 1–6, with sticky notes explaining each section.
3. Setup steps: credentials, environment variables, Zoho token, public URL, first-start `mail_cutoff`.
4. A test plan you have run, at least: a file with valid, invalid-phone, missing-message-date and `NOW` rows → ack lists the
   invalid rows; a NOW slot finishing → one update email in the same thread; all rows finished → FINAL; the same file
   again → "already received"; a non-allowed sender → ignored and left unread; a wrong webhook secret → 404; a duplicate
   webhook → ignored; a 401 from Sarvam → calls paused, rows still waiting; a tampered recording link → 404.

Reference implementation (same behaviour, Python): `ams/sheet.py` (reading/validation), `ams/campaigns.py` (row states,
retries, batch decisions), `ams/mailer.py` (inbox and emails), `ams/excel_out.py` (result file), `ams/scheduler.py`
(dispatcher), `ams/server.py` + `ams/links.py` (webhook, recording links), `ams/zoho.py`, `ams/sarvam.py`.
