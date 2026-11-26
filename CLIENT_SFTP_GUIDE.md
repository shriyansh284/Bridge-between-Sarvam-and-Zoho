# AMS Calling: uploading candidate files by SFTP

You upload a candidate file to a folder on our server. Our AI agent calls each candidate at the date and time in the
file, and the results appear in the same folder. No email is needed.

## Connecting

| | |
|---|---|
| Protocol | **SFTP** (in FileZilla or WinSCP choose SFTP, not FTP or FTPS) |
| Server, port | sent to you separately |
| Username | sent to you separately |
| Password or key | the password is sent separately; for an automated upload, send us your SSH **public** key instead |

Logins are accepted only from the IP addresses you give us.

## Folders

| Folder | What it's for |
|---|---|
| `inbox/` | Upload your files here. |
| `send/` | The output sheet. Within about a minute of your upload, `<your file name>_<campaign>_RESULTS.csv` appears here: each candidate's scheduled call time, then each call's result as it finishes (it is replaced after each scheduled call time). When every call is done it is replaced by `<your file name>_<campaign>_FINAL.csv`. |
| `failed/` | `<your file name>_<campaign>_MISSED_CALLS.csv`: every candidate we couldn't reach (no answer or busy after the retry, failed, not called because of a problem in the row), with the reason. It is kept up to date with the output sheet. A file we can't use at all is also moved here, with a note saying what to fix (for `calls.csv`, the note is `calls.csv_ERROR.txt`). |

Once a file is picked up it leaves `inbox/`; the RESULTS file appearing in `send/` is your confirmation. You may
delete files from `send/` and `failed/` once you've read them.

## Uploading

- Upload whenever you like, as often as you like. Each file can have any number of records, with different call
  dates and times. **Every record is called at its own date and time**: a file uploaded at 10:30 AM with a record for
  11:15 AM the same day gets that call at 11:15 AM. Use a new file name each time, for example with the date and time
  in it (`calls_2026-11-26_1030.csv`).
- **For automated uploads, upload under a temporary name and rename the file when the upload is complete**, for
  example upload `calls.csv.tmp`, then rename it to `calls.csv`. Files ending in `.tmp`, `.part` or `.filepart`, or
  starting with a dot, are never read. (Without this, a file is read once it has stopped changing for one minute; an
  upload that stalls for longer could be read half-way.)
- Uploaded files must be readable by our system: the usual permissions (`rw-rw----` / 660, or 644) are fine. A file
  uploaded as private (600) can't be read and is moved to `failed/` with a note.
- **Nobody is called twice for the same booking.** A record that comes again in a later file (same phone number, same
  call date and time) is skipped; its Remarks say which earlier upload has it. Uploading exactly the same file again
  within 7 days is also skipped (it goes to `failed/` with a note).

## File format

CSV, UTF-8, comma-separated, **with a header row** (the first line holds the column names). We find columns by their
names, so their order doesn't matter, but a file without a header row can't be used.

Your calling form's fields can be exported as they are:

| Your form's field | Required | What our agent gets | Example |
|---|---|---|---|
| Person to Call | Yes | The name it says | Meera Shah |
| Phone Number to Call | Yes | The number it calls: an Indian mobile number; +91 and spaces are fine | +91 98765 43210 |
| Call on Behalf of | Yes | The hiring company | ABC Ltd |
| Reason for Calling | Yes | **The pending documents**, which the agent asks the candidate for. Write the documents themselves, separated by commas | PAN Card, Graduation Certificate |
| WhatsApp Message Date | Yes | The date the WhatsApp reminder was sent | 11/25/2026 |
| Date to Call | Yes | When to call (date) | 11/26/2026 |
| Time to Call | Yes | When to call (India time) | 11:15am, 05:00pm or 17:00; NOW calls straight away |

- One **Date and Time To Call** column (`11/26/2026 11:15am`) works instead of two.
- **Dates:** month first like your form (11/25/2026), day first (25-11-2026) or `YYYY-MM-DD` all work. We work out
  the order from the file itself, so a file must use one order throughout; a file that mixes them is refused as a
  whole, so no call goes out on a wrong day.
- AI Call Agent Persona, AI Call Category and any other columns are kept and returned unchanged in the results.
- Rows with a problem (for example an empty Reason for Calling) are listed in the results with the reason, using your
  column's name, and aren't called. The other rows go ahead.
- Our own template's column names also work: Candidate Name, Phone Number, Hiring Company, Pending Documents,
  WhatsApp Message Date, Call Date, Call Time.

Calls are placed between 9:00 AM and 9:00 PM India time. A time outside those hours moves to the next morning. A time
that has already passed (within the last 24 hours) is called straight away; older times aren't called.

## Results

The results file has all your columns, then these:

Call Status, Call Date & Time, Call Duration, Attempt Count, Candidate Response, Commitment & Timeline, Call Summary,
Call Quality, Other Agent Outputs, Remarks, Recording Link (opens the call recording for 14 days), Transcript.

Call Status is one of: Scheduled, Calling, Connected, No Answer, Busy, Failed, Not called (invalid row), Cancelled,
Result pending (under review). A candidate who doesn't answer, or is busy, is called once more an hour later.
