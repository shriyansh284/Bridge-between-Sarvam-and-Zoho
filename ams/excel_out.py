"""
Build the result workbook the client receives.

For .xlsx input the client's own file is reopened and the result columns are
added to the right of their table, so their formatting, column order, other
sheets and extra columns stay exactly as they sent them. CSV/XLS input is
copied into a fresh sheet first. A "Call Summary" sheet is added at the end.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .campaigns import ATTEMPT_LABELS, ROW_LABELS, counts, next_call_time, slot_label
from .config import settings
from .links import recording_link
from .sheet import clean, read_sheet
from .store import campaign_dir
from .timeutil import fmt_local

RESULT_FILL = PatternFill("solid", start_color="DDEBF7")
HEAD_FILL = PatternFill("solid", start_color="1F3A5F")
CELL_LIMIT = 32000


def format_duration(seconds) -> str:
    if seconds in (None, ""):
        return ""
    s = int(round(float(seconds)))
    return f"{s // 60} min {s % 60:02d} sec" if s >= 60 else f"{s} sec"


def humanize(value) -> str:
    """Agent codes like 'agreed_to_submit' -> 'Agreed to submit'; free text is left as it is."""
    text = str(value or "").strip()
    return text.replace("_", " ").capitalize() if re.fullmatch(r"[a-z][a-z0-9_]*", text) else text


def transcript_text(transcript) -> str:
    if not transcript:
        return ""
    lines = []
    for turn in transcript:
        who = "Agent" if str(turn.get("role", "")).lower() in ("agent", "assistant", "bot") else "Candidate"
        lines.append(f"{who}: {turn.get('indic_text') or turn.get('en_text') or turn.get('text') or ''}")
    return "\n".join(lines)[:CELL_LIMIT]


def result_columns() -> list[str]:
    return (["Call Status", "Call Date & Time", "Call Duration", "Attempt Count"]
            + list(settings.result_field_map) + ["Other Agent Outputs", "Remarks", "Recording Link", "Transcript"])


def result_values(camp: dict, row: dict) -> dict:
    """Values for the appended columns of one candidate row."""
    attempts = [a for a in row["attempts"] if a["status"] != "trigger_error"]
    last = attempts[-1] if attempts else None
    connected = next((a for a in reversed(attempts) if a["status"] == "connected"), None)
    shown = connected or last

    status = ROW_LABELS.get(row["status"], row["status"])
    if row["status"] == "not_reachable" and last:
        status = ATTEMPT_LABELS.get(last["status"], status)

    outputs = dict((connected or {}).get("variables") or {})
    values = {
        "Call Status": status,
        "Call Date & Time": fmt_local(shown["triggered_at"]) if shown else "",
        "Call Duration": format_duration(shown.get("duration")) if shown else "",
        "Attempt Count": row.get("attempt_count", 0),
    }
    for column, key in settings.result_field_map.items():
        values[column] = humanize(outputs.pop(key, ""))
    for key in settings.agent_variable_map:  # inputs echoed back by the agent
        outputs.pop(key, None)
    values["Other Agent Outputs"] = "; ".join(f"{k}: {v}" for k, v in outputs.items() if v not in (None, ""))
    remark = row.get("note") or ""
    if row["status"] in ("scheduled", "retry_scheduled") and row.get("next_call_at"):
        remark = f"Call scheduled for {fmt_local(row['next_call_at'])}" + (f" ({remark})" if remark and "retrying" not in remark else "")
    values["Remarks"] = remark
    link = ""
    if connected and settings.public_base_url and (connected.get("interaction_id") or connected.get("recording_url")):
        link = recording_link(camp["campaign_id"], row["row_id"], connected["ref"])  # through this server, 30 days
    elif connected:
        link = connected.get("audio_url") or ""  # Sarvam's own signed link (no public address needed), 14 days
    values["Recording Link"] = link
    values["Transcript"] = transcript_text((connected or {}).get("transcript"))
    return values


def _style_header(cell, result: bool) -> None:
    cell.font = Font(name="Arial", bold=True, color="1F3A5F" if result else "FFFFFF")
    cell.fill = RESULT_FILL if result else HEAD_FILL
    cell.alignment = Alignment(vertical="center", wrap_text=True)


def _base_workbook(camp: dict):
    """Return (workbook, worksheet, header_row) holding the client's original data."""
    path = campaign_dir(camp["campaign_id"]) / camp["input_file"]
    if camp["file_kind"] == "xlsx":
        wb = load_workbook(path)  # formulas and formatting kept
        ws = wb[camp["sheet_name"]] if camp.get("sheet_name") in wb.sheetnames else wb.active
        return wb, ws, camp["header_row"]
    sheet = read_sheet(path.read_bytes(), camp["file_name"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append(sheet.headers)
    for cell in ws[1]:
        _style_header(cell, result=False)
    for _, cells in sheet.rows:
        ws.append(list(cells[: len(sheet.headers)]))
    return wb, ws, 1


def build_results(camp: dict, final: bool = False) -> bytes:
    wb, ws, header_row = _base_workbook(camp)
    headers = {str(ws.cell(header_row, c).value or "").strip(): c for c in range(1, ws.max_column + 1)}
    last_col = max((c for name, c in headers.items() if name), default=0)

    col_for: dict[str, int] = {}
    for name in result_columns():
        if name in headers:  # the client re-sent an earlier result file: overwrite, don't duplicate
            col_for[name] = headers[name]
        else:
            last_col += 1
            col_for[name] = last_col
            ws.cell(header_row, last_col, name)
        _style_header(ws.cell(header_row, col_for[name]), result=True)

    if camp["file_kind"] != "xlsx":
        # csv: data rows were appended in order, skipping blank lines; map by position
        positions = {r["excel_row"]: header_row + 1 + i
                     for i, r in enumerate(sorted(camp["rows"], key=lambda r: r["excel_row"]))}
    else:
        positions = {r["excel_row"]: r["excel_row"] for r in camp["rows"]}

    for row in camp["rows"]:
        target = positions[row["excel_row"]]
        for name, value in result_values(camp, row).items():
            cell = ws.cell(target, col_for[name], value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=name in ("Transcript", "Other Agent Outputs", "Remarks",
                                                                         "Call Summary", "Commitment & Timeline"))
            if name == "Recording Link" and value:
                cell.hyperlink = value
                cell.value = "Listen"
                cell.font = Font(name="Arial", size=10, color="0563C1", underline="single")

    widths = {"Call Status": 18, "Call Date & Time": 22, "Call Duration": 14, "Attempt Count": 10,
              "Commitment & Timeline": 30, "Call Summary": 50, "Call Quality": 14,
              "Other Agent Outputs": 40, "Remarks": 40, "Recording Link": 12, "Transcript": 70}
    for name, col in col_for.items():
        ws.column_dimensions[get_column_letter(col)].width = widths.get(name, 24)

    _summary_sheet(wb, camp, final)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _summary_sheet(wb, camp: dict, final: bool) -> None:
    if "Call Summary" in wb.sheetnames:
        del wb["Call Summary"]
    ws = wb.create_sheet("Call Summary")
    c = counts(camp)
    nxt: Optional[str] = next_call_time(camp)
    rows = [
        ("Campaign ID", camp["campaign_id"]),
        ("File", camp["file_name"]),
        ("Received", fmt_local(camp["created_at"])),
        ("Report type", "Final (all calls finished)" if final else "Interim update"),
        ("Total rows", c["total"]),
        ("Calls finished", f"{c['done']} of {c['valid']}"),
        ("Connected", c["connected"]),
        ("No answer / busy", c["no_answer"]),
        ("Failed", c["failed"]),
        ("Result pending (under review)", c["review"]),
        ("Cancelled", c["cancelled"]),
        ("Not called (invalid rows)", c["invalid"]),
        ("Still scheduled", c["waiting"] + c["calling"]),
        ("Next scheduled call", fmt_local(nxt) if nxt else "None"),
    ]
    slots = sorted({r["slot"] for r in camp["rows"] if r["status"] != "invalid"})
    if slots:
        rows.append(("Scheduled call times", ", ".join(slot_label(s) for s in slots)))
    for label, value in rows:
        ws.append([label, value])
    for r in ws.iter_rows():
        r[0].font = Font(name="Arial", bold=True)
        r[1].font = Font(name="Arial")
        r[1].alignment = Alignment(wrap_text=True, horizontal="left")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 60


def build_results_csv(camp: dict) -> bytes:
    """The same result columns as build_results, as a CSV (for the SFTP folder, where the client's
    system reads the file). The client's own columns come first, unchanged; links are plain URLs."""
    path = campaign_dir(camp["campaign_id"]) / camp["input_file"]
    sheet = read_sheet(path.read_bytes(), camp["file_name"])
    width = max((i + 1 for i, h in enumerate(sheet.headers) if h), default=0)  # drop empty trailing columns
    headers = list(sheet.headers[:width])
    col_for: dict[str, int] = {}
    for name in result_columns():
        if name in headers:  # the client re-sent an earlier result file: overwrite, don't duplicate
            col_for[name] = headers.index(name)
        else:
            headers.append(name)
            col_for[name] = len(headers) - 1

    by_row = {r["excel_row"]: r for r in camp["rows"]}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    for excel_row, cells in sheet.rows:
        # text stays exactly as sent; Excel numbers and dates are written the way the sheet reads them
        line = [c if isinstance(c, str) else clean(c) for c in list(cells[:width]) + [None] * (width - len(cells[:width]))]
        line += [""] * (len(headers) - len(line))
        row = by_row.get(excel_row)
        if row:
            for name, value in result_values(camp, row).items():
                line[col_for[name]] = "" if value is None else str(value)
        writer.writerow(line)
    return buf.getvalue().encode("utf-8-sig")  # with a BOM, so Excel shows Indian names correctly


def output_name(camp: dict, kind: str, update_no: Optional[int] = None, ext: str = "xlsx") -> str:
    base = f"AMS_Calling_Results_{camp['campaign_id']}"
    if kind == "final":
        return f"{base}_FINAL.{ext}"
    if kind == "update":
        return f"{base}_Update-{update_no or 1}.{ext}"
    return f"{base}.{ext}"
