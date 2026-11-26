"""
The client's SFTP folder: the client's system uploads candidate files, and collects the results, in a folder on
this server. deploy/setup_sftp.sh creates the folder and the client's SFTP login (which can see nothing else on
the server) and sets DROP_DIR in .env.

DROP_DIR/
  inbox/   the client uploads files here (.csv or .xlsx, with a header row). A file leaves the inbox as soon as
           it's picked up (the original is kept in data/campaigns/<campaign>/).
  send/    the output sheet: <file>_<campaign>_RESULTS.csv right away (the schedule), replaced after every
           finished call time, then <file>_<campaign>_FINAL.csv once every call is done
  failed/  <file>_<campaign>_MISSED_CALLS.csv: every candidate not reached (no answer, busy, failed, not called),
           kept up to date; and files that can't be used, each with a note (<file name>_ERROR.txt)

poll_drop() runs every DROP_POLL_SECONDS. A file is read only once it has stopped changing for
DROP_SETTLE_SECONDS, so a file still being uploaded is never read half-way, and names upload tools use while
uploading (.filepart, .part, .tmp, names starting with a dot) are skipped. Every file this app writes appears
under its final name in one step: it is written under a hidden temporary name first, then renamed.

Crash safety: each pickup is recorded in data/state/drop_files.json, then the file is claimed by renaming it to a
hidden name in the inbox (.picked-...), and the campaign keeps the record's id. A restart at any point finishes the
pickup without ever creating the campaign (and calling its candidates) twice.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Optional

from filelock import Timeout

from .campaigns import NOT_CONNECTED, counts, create_campaign, file_sha, find_drop_campaign, find_duplicate_drop
from .config import settings
from .excel_out import build_results, build_results_csv, build_table_xlsx, output_name, result_table
from .sheet import CLIENT_FORMAT, SheetError
from .store import STATE, _windows_retry, append_log, campaign_dir, lock, read_json, set_status, write_json
from .timeutil import fmt_local, iso, to_local, utcnow

log = logging.getLogger(__name__)
INBOX, SEND, FAILED = "inbox", "send", "failed"
FOLDERS = (INBOX, SEND, FAILED)
PICKED = ".picked-"  # a claimed file's hidden name in the inbox, while its campaign is being created
SPREADSHEET_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
TEMP_SUFFIXES = (".filepart", ".part", ".partial", ".tmp", ".temp", ".crdownload", ".upload", ".uploading")
MAX_FILE_BYTES = 25 * 1024 * 1024
CLAIMS = STATE / "drop_files.json"
SOURCE = "SFTP upload"  # how uploads appear in the dashboard's File log


class DropError(Exception):
    """The drop folder itself can't be used (missing, or this app has no access)."""


def enabled() -> bool:
    return bool(settings.drop_dir)


def folder(name: str) -> Path:
    return Path(settings.drop_dir) / name


def check_folders() -> None:
    if not settings.drop_dir:  # Path("") would be the app's own folder
        raise DropError("This campaign came through the SFTP folder, but DROP_DIR isn't set in .env any more. "
                        "Set it again and restart the backend; the results are sent then.")
    root = Path(settings.drop_dir)
    if not root.is_absolute():
        raise DropError(f"DROP_DIR must be a full path, e.g. /srv/ams-sftp/amsclient (it is '{settings.drop_dir}').")
    if not root.is_dir():
        raise DropError(f"The SFTP folder {root} doesn't exist. Run deploy/setup_sftp.sh, or fix DROP_DIR in .env.")
    for name in FOLDERS:
        path = root / name
        if not path.is_dir():
            try:
                path.mkdir()
            except OSError as exc:
                raise DropError(f"{path} is missing and can't be created ({exc.strerror}). "
                                "Run deploy/setup_sftp.sh again.") from exc
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise DropError(f"This app can't read and write {path}. Run deploy/setup_sftp.sh again (it gives the "
                            "app's account access to the folders), then restart the backend.")


# ---------------------------------------------------------------- small helpers
def _write(dest: Path, data: bytes) -> None:
    """Appear under the final name in one step, so the client never picks up a half-written file."""
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o640)  # group (the client's SFTP account) may read it
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _windows_retry(lambda: os.replace(tmp, dest))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ignored(name: str) -> bool:
    return name.startswith((".", "~$")) or name.lower().endswith(TEMP_SUFFIXES)


def _changed_at(st: os.stat_result) -> float:
    # Upload tools can set a file's modified time ("preserve timestamps"); the change time (ctime) can't be
    # set and moves with every write. On Windows (local testing) ctime is the creation time, so use mtime.
    return st.st_mtime if os.name == "nt" else st.st_ctime


def _free_name(directory: Path, name: str) -> str:
    """`name`, or with the time added when a file of that name is already there."""
    if not os.path.lexists(directory / name):
        return name
    stem, ext = os.path.splitext(name)
    stamp = to_local(utcnow()).strftime("%Y%m%d-%H%M%S")
    for i in range(1, 1000):
        candidate = f"{stem}_{stamp}{'' if i == 1 else f'-{i}'}{ext}"
        if not os.path.lexists(directory / candidate):
            return candidate
    return f"{stem}_{uuid.uuid4().hex[:8]}{ext}"


def _claims() -> dict:
    return read_json(CLAIMS, {})


def _record(claim_id: str, **fields) -> dict:
    data = _claims()
    data.setdefault(claim_id, {}).update(fields)
    write_json(CLAIMS, data)
    return data[claim_id]


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise SheetError("This is not a regular file.")
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SheetError("The file is larger than 25 MB. Split it into smaller files.")
    return data


def _format_rules() -> list[str]:
    lines = ["The file must have these columns, in this order (one candidate per row, header in the first row):"]
    for column, required, rule, example in CLIENT_FORMAT:
        lines.append(f"  - {column} ({'required' if required == 'Yes' else 'optional'}): {rule} Example: {example}")
    return lines


def _text(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")  # opens cleanly in Notepad and Excel


FIX_AND_RESEND = "Correct the file and upload it to the inbox folder again."


# ---------------------------------------------------------------- inbound
def poll_drop() -> dict:
    """One pass over DROP_DIR/inbox. Safe to call from several processes (only one runs at a time)."""
    stats = {"files": 0, "campaigns": 0, "rejected": 0, "waiting": 0}
    if not enabled():
        return stats
    try:
        with lock("drop-poll", timeout=0):
            errors: list[str] = []
            try:
                check_folders()
                for claim_id, rec in list(_claims().items()):  # pickups a restart interrupted
                    if rec.get("status") in ("claiming", "claimed", "rejecting"):
                        try:
                            outcome = _resume(claim_id)
                            if outcome:
                                stats[outcome] = stats.get(outcome, 0) + 1
                        except Exception as exc:  # one stuck file must never block the others
                            log.exception("SFTP folder: %s", rec.get("file"))
                            errors.append(f"{rec.get('file')}: {exc}")
                _scan_inbox(stats, errors)
            except Exception as exc:  # the folder itself (missing, no access): reported, retried next pass
                errors.append(str(exc))
            for e in errors:
                log.error("SFTP folder: %s", e)
            set_status("drop_last_check", {"at": iso(utcnow()), "ok": not errors, "waiting": stats["waiting"],
                                           "error": errors[0][:500] if errors else None})
    except Timeout:
        pass  # another process is checking the folder right now
    return stats


def _scan_inbox(stats: dict, errors: list[str]) -> None:
    now = time.time()
    entries = []
    for path in folder(INBOX).iterdir():
        if _ignored(path.name):
            continue
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            continue  # a folder the client made; only files are picked up
        if now - _changed_at(st) < settings.drop_settle_seconds:
            stats["waiting"] += 1  # still being uploaded (or only just finished)
            continue
        entries.append((_changed_at(st), path.name, path))
    for _, _, path in sorted(entries):  # oldest upload first
        stats["files"] += 1
        try:
            outcome = _pick_up(path)
            stats[outcome] = stats.get(outcome, 0) + 1
        except Exception as exc:
            log.exception("SFTP folder: %s", path.name)
            errors.append(f"{path.name}: {exc}")


def _pick_up(path: Path) -> str:
    claim_id = uuid.uuid4().hex
    claimed_as = f"{PICKED}{claim_id[:12]}-{path.name}"  # hidden: never picked up a second time
    _record(claim_id, file=path.name, claimed_as=claimed_as, status="claiming", at=iso(utcnow()))
    try:
        os.rename(path, folder(INBOX) / claimed_as)  # same folder, so the rename is one step
    except FileNotFoundError:  # the client removed it again
        data = _claims()
        data.pop(claim_id, None)
        write_json(CLAIMS, data)
        return "waiting"
    _record(claim_id, status="claimed")
    return _finish(claim_id)


def _resume(claim_id: str) -> Optional[str]:
    rec = _claims()[claim_id]
    if rec["status"] == "claiming":
        if not os.path.lexists(folder(INBOX) / rec["claimed_as"]):
            data = _claims()  # never renamed: still in the inbox under its own name, picked up normally
            data.pop(claim_id, None)
            write_json(CLAIMS, data)
            return None
        _record(claim_id, status="claimed")
    if rec["status"] == "rejecting":
        return _reject(claim_id, rec["reason"], rec.get("advice"))
    return _finish(claim_id)


def _finish(claim_id: str) -> str:
    rec = _claims()[claim_id]
    src = folder(INBOX) / rec["claimed_as"]
    existing = find_drop_campaign(claim_id)
    if existing:  # created just before a restart
        src.unlink(missing_ok=True)
        _record(claim_id, status="campaign", campaign=existing)
        return "campaigns"
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        _record(claim_id, status="lost")
        append_log("file_log", {"from": SOURCE, "file": rec["file"], "result": "ignored",
                                "reason": "The file was removed from the inbox before it could be read"})
        return "rejected"
    if not stat.S_ISREG(st.st_mode):
        return _reject(claim_id, "This is a link or a special file, not a normal file.", "Upload the file itself.")
    if os.name != "nt" and st.st_nlink > 1:
        return _reject(claim_id, "Linked files aren't accepted.", "Upload the file itself.")
    if not rec["file"].lower().endswith(SPREADSHEET_EXT):
        return _reject(claim_id, "Only .csv and .xlsx files are processed.",
                       "Save the candidate list as .csv or .xlsx and upload it to the inbox folder again.")
    try:
        data = _read(src)
    except SheetError as exc:
        return _reject(claim_id, str(exc))
    except PermissionError:  # uploaded with its own permissions (e.g. 600), so only the uploader can read it
        return _reject(claim_id, "Our system isn't allowed to read this file: it was uploaded with permissions "
                                 "that let only your login read it.",
                       "Upload it again with read permission for the group (rw-rw---- / 660, or 640). In WinSCP, "
                       "turn off \"Set permissions\" in the transfer settings; in a script, set the mode to 660.")
    except OSError as exc:
        return _reject(claim_id, f"This file couldn't be opened ({exc.strerror or exc}).", "Upload the file itself again.")
    dup = find_duplicate_drop(file_sha(data))
    if dup:
        return _reject(claim_id, f"This exact file was already received as campaign {dup}, so its candidates "
                                 "were not called again.",
                       "If they should be called again, change the call times in the file and upload it again.")
    drop = {"claim_id": claim_id, "file": rec["file"]}
    try:
        camp = create_campaign(data, rec["file"], source="sftp", drop=drop)
    except SheetError as exc:
        return _reject(claim_id, str(exc))
    except OSError:
        raise  # our own data folder (disk full?): the claimed file waits in the inbox, retried next pass
    except Exception as exc:  # a file the reader chokes on: tell the client instead of retrying forever
        log.exception("SFTP folder: couldn't read %s", rec["file"])
        existing = find_drop_campaign(claim_id)
        if existing:
            src.unlink(missing_ok=True)
            _record(claim_id, status="campaign", campaign=existing)
            return "campaigns"
        return _reject(claim_id, f"This file couldn't be read ({exc.__class__.__name__}).",
                       "Save it again as a UTF-8 .csv (or .xlsx) with the header row, and upload it to the inbox "
                       "folder again.")
    src.unlink(missing_ok=True)  # the original is kept in data/campaigns/<campaign>/
    _record(claim_id, status="campaign", campaign=camp["campaign_id"])
    append_log("file_log", {"from": SOURCE, "file": rec["file"], "result": "campaign",
                            "campaign": camp["campaign_id"], "rows": len(camp["rows"]), "status": camp["status"]})
    return "campaigns"


def _reject(claim_id: str, reason: str, advice: Optional[str] = None) -> str:
    """Move the file to failed/ with a note. Safe to repeat after a restart."""
    rec = _claims()[claim_id]
    moved_to = rec.get("moved_to") or _free_name(folder(FAILED), rec["file"])
    if rec["status"] != "rejecting":
        rec = _record(claim_id, status="rejecting", moved_to=moved_to, reason=reason, advice=advice)
    src = folder(INBOX) / rec["claimed_as"]
    if os.path.lexists(src):
        os.rename(src, folder(FAILED) / moved_to)
    lines = [f"File: {rec['file']}", f"Received: {fmt_local(rec['at'])} (India time)",
             "Result: not processed. No calls were placed.", "", f"Problem: {reason}", "",
             f"What to do: {advice or FIX_AND_RESEND}"]
    if not advice:
        lines += [""] + _format_rules()
    _write(folder(FAILED) / f"{moved_to}_ERROR.txt", _text(lines))
    _record(claim_id, status="rejected")
    append_log("file_log", {"from": SOURCE, "file": rec["file"], "result": "rejected", "reason": reason})
    return "rejected"


# ---------------------------------------------------------------- outbound
def result_formats() -> list[str]:
    return {"csv": ["csv"], "xlsx": ["xlsx"], "both": ["csv", "xlsx"]}.get(settings.drop_result_format, ["csv"])


def result_base(camp: dict) -> str:
    stem = Path((camp.get("drop") or {}).get("file") or camp["file_name"]).stem.strip()
    stem = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", stem)[:80] or "file"
    return f"{stem}_{camp['campaign_id']}"


def deliver(camp: dict, rec: dict) -> str:
    """Write one queued result file into the client's SFTP folder:
    ack/update -> send/<base>_RESULTS, final -> send/<base>_FINAL (the RESULTS file is then removed), and with
    each of them failed/<base>_MISSED_CALLS for the candidates not reached so far;
    rejected (no row could be called) -> failed/<base>_ERRORS plus a note.
    Returns the copy kept in the campaign's outputs/ for the dashboard."""
    if not camp.get("drop"):  # never write another campaign's results into the client's folder
        raise DropError(f"{camp['campaign_id']} didn't come through the SFTP folder")
    check_folders()
    kind, base = rec["kind"], result_base(camp)
    final = kind == "final"
    built = {ext: build_results_csv(camp) if ext == "csv" else build_results(camp, final=final)
             for ext in result_formats()}
    if kind == "rejected":
        for ext, data in built.items():
            _write(folder(FAILED) / f"{base}_ERRORS.{ext}", data)
        _write(folder(FAILED) / f"{base}_ERRORS.txt", _text(_rejected_note(camp, base)))
    else:
        label = "FINAL" if final else "RESULTS"
        for ext, data in built.items():
            _write(folder(SEND) / f"{base}_{label}.{ext}", data)
        if final:  # one file per campaign: the in-progress one is replaced by the final one
            for ext in ("csv", "xlsx"):
                (folder(SEND) / f"{base}_RESULTS.{ext}").unlink(missing_ok=True)
        _write_missed(camp, base)
    ext = next(iter(built))
    saved = output_name(camp, "check" if kind == "rejected" else kind, rec.get("update_no"), ext)
    out = campaign_dir(camp["campaign_id"]) / "outputs"
    out.mkdir(exist_ok=True)
    (out / saved).write_bytes(built[ext])
    return f"outputs/{saved}"


def _write_missed(camp: dict, base: str) -> None:
    """failed/<base>_MISSED_CALLS: the candidates whose calls are finished without reaching them (no answer, busy,
    failed, under review, not called), with the same columns as the output sheet. Rewritten with every output
    sheet, and removed if a late result means everyone was reached after all."""
    headers, lines = result_table(camp, NOT_CONNECTED)
    for ext in ("csv", "xlsx"):
        path = folder(FAILED) / f"{base}_MISSED_CALLS.{ext}"
        if not lines or ext not in result_formats():
            path.unlink(missing_ok=True)
        elif ext == "csv":
            _write(path, build_results_csv(camp, NOT_CONNECTED))
        else:
            _write(path, build_table_xlsx(headers, lines, "Missed calls"))


def _rejected_note(camp: dict, base: str) -> list[str]:
    c = counts(camp)
    lines = [f"File: {camp['file_name']}", f"Campaign: {camp['campaign_id']}",
             f"Received: {fmt_local(camp['created_at'])} (India time)",
             f"Result: none of the {c['total']} rows can be called, so no calls were placed.", "", "Problems:"]
    for r in camp["rows"][:200]:
        lines.append(f"  - Row {r['excel_row']} ({r['name'] or r['candidate_id']}): {'; '.join(r['errors'])}")
    lines += ["", f"{base}_ERRORS shows the problem next to each row (Remarks column).", "",
              f"What to do: {FIX_AND_RESEND}", ""] + _format_rules()
    return lines
