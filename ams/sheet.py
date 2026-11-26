"""
Read the client's attachment and validate every row.

Accepted files: .xlsx (preferred), .xlsm, .csv, .xls (needs xlrd).
The header row is found automatically in the first 15 rows, so a title line
above the table is fine. Column names are matched loosely (case, spaces,
underscores ignored), so both the AMS template headers and the old Sarvam
cohort headers (phone_number, name, Name_of_Hiring_company, ...) work.

Call time can be given as:
  Call Date + Call Time columns   20-11-2026 | 10:30 AM
  one combined column             20-11-2026 10:30   (or NOW)
"""
from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from openpyxl import load_workbook

from .config import settings
from .timeutil import fmt_local, local_to_utc, next_allowed_time, to_local, utcnow

# The AMS template's names first, then the old Sarvam cohort headers, then the field names of the client's
# own calling form ("Person to Call", "Call on Behalf of", "Phone Number to Call", "Date and Time To Call").
FIELD_ALIASES: dict[str, list[str]] = {
    "candidate_id": ["candidate_id", "candidate_ref", "candidate_code", "id", "ref", "reference", "emp_id",
                     "employee_id", "sr_no", "s_no"],
    "phone_number": ["phone_number", "phone", "mobile", "mobile_number", "mobile_no", "phone_no",
                     "contact_number", "contact_no", "contact", "phone_number_to_call", "number_to_call"],
    "candidate_name": ["candidate_name", "name", "candidate", "full_name", "person_to_call"],
    "hiring_company": ["hiring_company", "name_of_hiring_company", "company", "company_name", "co", "client_company",
                       "call_on_behalf_of", "on_behalf_of"],
    # the client's form has no documents field: "Reason for Calling" carries them (used only without the above)
    "pending_documents": ["pending_documents", "pending_document_names", "pending_document", "pending_docs",
                          "documents", "docs", "doc", "reason_for_calling", "reason_for_call", "call_reason"],
    "whatsapp_message_date": ["whatsapp_message_date", "message_date", "msg_date", "whatsapp_date",
                              "whatsapp_message_date_date"],
    "call_datetime": ["call_datetime", "call_date_time", "scheduled_call_datetime", "scheduled_datetime",
                      "schedule", "call_schedule", "date_and_time_to_call", "date_time_to_call",
                      "call_date_and_time"],
    "call_date": ["call_date", "scheduled_call_date", "scheduled_date", "date_of_call", "date_to_call",
                  "date_and_time_to_call_date"],
    "call_time": ["call_time", "scheduled_call_time", "scheduled_time", "time_of_call", "time_to_call",
                  "date_and_time_to_call_time"],
    "initial_language_name": ["initial_language_name", "language", "call_language"],
    "initial_bot_message": ["initial_bot_message", "opening_line"],
    "initial_state_name": ["initial_state_name"],
    "additional_context": ["additional_context", "context", "notes", "remarks"],
}
# Every value the agent needs to make the call (agent variables name, phone_number, Name_of_Hiring_company,
# pending_document_names, whatsapp_message_date). A row missing one is reported and not called.
REQUIRED = {"phone_number": "Phone Number", "candidate_name": "Candidate Name",
            "hiring_company": "Hiring Company", "pending_documents": "Pending Documents",
            "whatsapp_message_date": "WhatsApp Message Date"}

# The predefined client format: only what the agent needs, plus when to call.
# (column, required?, how to fill it, example). templates/ are built from this: python -m ams.make_templates
# Extra columns a client adds (e.g. their own ID) are kept and returned unchanged, but aren't needed.
CLIENT_FORMAT = [
    ("Candidate Name", "Yes", "Name as the agent should say it.", "Rahul Sharma"),
    ("Phone Number", "Yes", "10-digit mobile number, without +91 or 91 "
                            "(Excel turns longer numbers into 9.19E+11 and breaks them).", "9876543210"),
    ("Hiring Company", "Yes", "Company the candidate is joining.", "ABC Company"),
    ("Pending Documents", "Yes", "Documents still pending, separated by commas.", "PAN Card, Graduation Certificate"),
    ("WhatsApp Message Date", "Yes", "Date the WhatsApp reminder was sent, DD-MM-YYYY.", "18-11-2026"),
    ("Call Date", "Yes", "Date to call, DD-MM-YYYY. Can be empty when Call Time is NOW.", "20-11-2026"),
    ("Call Time", "Yes", "India time, e.g. 10:30 AM or 16:00. Write NOW to call as soon as the email arrives.",
     "10:30 AM"),
]
TEMPLATE_HEADERS = [column for column, *_ in CLIENT_FORMAT]
SARVAM_LANGUAGES = {"Bengali", "Gujarati", "Kannada", "Malayalam", "Tamil", "Telugu", "Punjabi",
                    "Sanskrit", "Odia", "Marathi", "Hindi", "English", "Assamese"}
NOW_WORDS = {"NOW", "ASAP", "IMMEDIATE", "IMMEDIATELY"}

DATETIME_FORMATS = [
    "%d-%m-%Y %H:%M", "%d-%m-%y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%d.%m.%Y %H:%M",
    "%d-%m-%Y %I:%M %p", "%d-%m-%y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d/%m/%y %I:%M %p",
    "%d-%m-%Y %I %p", "%d/%m/%Y %I %p", "%d-%m-%Y %I%p", "%d/%m/%Y %I%p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d %b %Y %H:%M", "%d-%b-%Y %I:%M %p",
]
DATE_FORMATS = ["%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
                "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %B %Y"]
TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p", "%I %p", "%I%p", "%I:%M%p", "%H.%M"]


class SheetError(Exception):
    """The file can't be used at all (unreadable, no header, missing columns)."""


# ---------------------------------------------------------------- small helpers
def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")


def clean(value) -> str:
    """Cell value -> trimmed text (numbers lose '.0', blanks become '')."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M") if (value.hour or value.minute) else value.strftime("%d-%m-%Y")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat", "null"} else text


def normalize_phone(raw) -> Optional[str]:
    """Indian mobile -> +91XXXXXXXXXX. None when the number can't be trusted."""
    text = clean(raw)
    if not text or re.search(r"e\+?\d+$", text, re.IGNORECASE):  # Excel scientific notation loses digits
        return None
    if text.endswith(".0"):
        text = text[:-2]
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return "+91" + digits[1:]
    if len(digits) == 13 and digits.startswith("091") and digits[3] in "6789":
        return "+" + digits[1:]
    if has_plus and 8 <= len(digits) <= 15 and not digits.startswith("91"):
        return "+" + digits
    return None


def is_now(value) -> bool:
    return clean(value).upper() in NOW_WORDS


def _formats(formats: list[str], order: str) -> list[str]:
    """Numeric dates are day first (DD-MM-YYYY) unless the file's source is set to month first ("mdy",
    as in 11/25/2026). Dates written with a month name or as YYYY-MM-DD read the same either way."""
    if order != "mdy":
        return formats
    return [f.replace("%d-%m", "%m-%d").replace("%d/%m", "%m/%d").replace("%d.%m", "%m.%d") for f in formats]


def _example(order: str) -> str:
    return "MM/DD/YYYY, e.g. 11/20/2026" if order == "mdy" else "DD-MM-YYYY, e.g. 20-11-2026"


def parse_datetime_text(text: str, order: str = "dmy") -> datetime:
    text = re.sub(r"\s+", " ", text.strip())
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}", text):  # ISO, maybe with a UTC offset (form exports)
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value.tzinfo:
                value = to_local(value).replace(tzinfo=None)
            return value.replace(second=0, microsecond=0)
        except ValueError:
            pass
    text = re.sub(r"(?i)\s*(am|pm)$", lambda m: " " + m.group(1).upper(), text)
    for fmt in _formats(DATETIME_FORMATS, order):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Can't read the call date/time '{text}'. Use {_example(order)} 10:30")


def parse_date(value, order: str = "dmy") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    iso_date = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?", text)
    if iso_date:
        try:
            return date.fromisoformat(iso_date.group(1))
        except ValueError:
            pass
    for fmt in _formats(DATE_FORMATS, order):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Can't read the call date '{text}'. Use {_example(order)}")


def parse_time(value) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, datetime):  # Excel time-only cells can arrive as 1899-12-30 10:30
        return value.time().replace(second=0, microsecond=0)
    try:
        fraction = float(value)  # Excel stores times as a fraction of a day
    except (TypeError, ValueError):
        fraction = None
    if fraction is not None and 0 <= fraction < 1:
        minutes = round(fraction * 24 * 60)
        return time(minutes // 60, minutes % 60)
    text = re.sub(r"\s+", " ", clean(value)).upper()
    if re.fullmatch(r"\d{1,2}\.\d{2}( ?[AP]M)?", text):  # 10.30 -> 10:30
        text = text.replace(".", ":")
    text = re.sub(r"(\d)(AM|PM)$", r"\1 \2", text)
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Can't read the call time '{clean(value)}'. Use HH:MM, e.g. 10:30 or 4:00 PM")


def normalize_message_date(value, order: str = "dmy") -> str:
    """The agent always gets DD-MM-YYYY, whatever order the file used."""
    if not clean(value):
        return ""
    try:
        return parse_date(value, order).strftime("%d-%m-%Y")
    except ValueError:
        return clean(value)


# ---------------------------------------------------------------- reading
@dataclass
class SheetData:
    kind: str                       # xlsx | csv | xls
    sheet_name: Optional[str]
    header_row: int                 # 1-based row of the header in the file
    headers: list[str]
    rows: list[tuple[int, list]]    # (1-based row number, raw cell values)
    mapping: dict[str, int] = field(default_factory=dict)  # field -> column index


def _match_headers(cells: list) -> dict[str, int]:
    normalized = [_norm(c) for c in cells]
    mapping: dict[str, int] = {}
    for fld, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            idx = next((i for i, n in enumerate(normalized) if n == alias and i not in mapping.values()), None)
            if idx is not None:
                mapping[fld] = idx
                break
    return mapping


def _find_header(all_rows: list[list]) -> tuple[int, dict[str, int]]:
    for i, cells in enumerate(all_rows[:15]):
        mapping = _match_headers(cells)
        if "phone_number" in mapping and len(mapping) >= 2:
            return i, mapping
    raise SheetError("Couldn't find the header row. The first rows must contain column names such as "
                     "Phone Number and Candidate Name.")


def read_sheet(data: bytes, filename: str) -> SheetData:
    name = filename.lower()
    sheets: list[tuple[Optional[str], list[list]]] = []
    if name.endswith((".xlsx", ".xlsm")):
        try:  # a damaged or half-uploaded workbook can fail while opening or while reading rows
            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            first = wb.active or wb.worksheets[0]
            order = [first.title] + [ws.title for ws in wb.worksheets if ws.title != first.title]
            for title in order:
                sheets.append((title, [list(r) for r in wb[title].iter_rows(values_only=True)]))
            wb.close()
        except Exception as exc:
            raise SheetError(f"This Excel file can't be opened ({exc.__class__.__name__}). "
                             "Save it again as .xlsx (or .csv) and resend it.") from exc
        kind = "xlsx"
    elif name.endswith(".csv"):
        utf16_bom = data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE)
        encodings = ("utf-16",) if utf16_bom else ("utf-8-sig", "cp1252")  # Excel's "Unicode text" is UTF-16
        for enc in encodings:
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise SheetError("The CSV file's text encoding isn't readable. Save it as UTF-8 or as .xlsx.")
        try:
            text = text.replace(chr(0), "")  # stray NUL characters make Python 3.10's csv module stop
            sheets.append((None, [list(r) for r in csv.reader(io.StringIO(text))]))
        except csv.Error as exc:
            raise SheetError(f"This CSV file can't be read ({exc}). Save it again as UTF-8 CSV.") from exc
        kind = "csv"
    elif name.endswith(".xls"):
        try:
            import pandas as pd

            df = pd.read_excel(io.BytesIO(data), header=None, dtype=object)
        except Exception as exc:
            raise SheetError("Old .xls files need converting. Please save the file as .xlsx and resend.") from exc
        sheets.append((None, df.where(df.notna(), None).values.tolist()))
        kind = "xls"
    else:
        raise SheetError("Attach the candidate list as .xlsx or .csv.")

    last_error: Optional[SheetError] = None
    for title, all_rows in sheets:
        try:
            idx, mapping = _find_header(all_rows)
        except SheetError as exc:
            last_error = exc
            continue
        headers = [clean(c) for c in all_rows[idx]]
        body = []
        for offset, cells in enumerate(all_rows[idx + 1:], start=idx + 2):
            cells = list(cells) + [None] * (len(headers) - len(cells))
            if any(clean(c) for c in cells):
                body.append((offset, cells))
        return SheetData(kind, title, idx + 1, headers, body, mapping)
    raise last_error or SheetError("The file has no rows.")


# ---------------------------------------------------------------- validation
@dataclass
class RowCheck:
    excel_row: int
    data: dict
    raw: list
    scheduled_local: Optional[datetime] = None
    scheduled_utc: Optional[datetime] = None
    effective_utc: Optional[datetime] = None
    slot: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def missing_columns(sheet: SheetData) -> list[str]:
    missing = [title for fld, title in REQUIRED.items() if fld not in sheet.mapping]
    if not ({"call_datetime", "call_date", "call_time"} & sheet.mapping.keys()) and settings.blank_time == "reject":
        missing.append("Call Date / Call Time")
    return missing


NUMERIC_DATE = re.compile(r"\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")
DATE_FIELDS = ("call_datetime", "call_date", "call_time", "whatsapp_message_date")


def detect_date_order(sheet: SheetData, default: str = "dmy") -> str:
    """How this file writes numeric dates. A date like 05/11/2026 reads two ways (5 Nov or 11 May), but 25/11/2026
    can only be day first and 11/25/2026 only month first. A file comes from one system, so one such date settles
    the whole file. With none (every date could be either) `default` is used: the template is day first, and
    DROP_DATE_ORDER says how the client's own system writes them. A file showing both orders is refused, so no
    call goes out on a wrong day."""
    columns = [sheet.mapping[f] for f in DATE_FIELDS if f in sheet.mapping]
    seen: dict[str, tuple[int, str]] = {}
    for excel_row, cells in sheet.rows:
        for idx in columns:
            value = cells[idx] if idx < len(cells) else None
            match = NUMERIC_DATE.match(value) if isinstance(value, str) else None  # Excel date cells are exact
            if not match:
                continue
            first, second = int(match.group(1)), int(match.group(2))
            if first <= 12 < second <= 31:
                seen.setdefault("mdy", (excel_row, value.strip()))
            elif second <= 12 < first <= 31:
                seen.setdefault("dmy", (excel_row, value.strip()))
    if len(seen) == 2:
        (r1, v1), (r2, v2) = seen["mdy"], seen["dmy"]
        raise SheetError(f"The dates in this file are written two ways: row {r1} has '{v1}' (month first) and row "
                         f"{r2} has '{v2}' (day first). Write every date the same way, DD-MM-YYYY (25-11-2026) or "
                         "YYYY-MM-DD. No row was called, so that no call goes out on a wrong day.")
    return next(iter(seen), default if default in ("dmy", "mdy") else "dmy")


def _schedule(sheet: SheetData, cells: list, received_utc: datetime,
              order: str = "dmy") -> tuple[Optional[datetime], bool]:
    """Returns (local datetime or None, called_now). Raises ValueError with a readable message."""
    m = sheet.mapping
    dt_val = cells[m["call_datetime"]] if "call_datetime" in m else None
    d_val = cells[m["call_date"]] if "call_date" in m else None
    t_val = cells[m["call_time"]] if "call_time" in m else None

    if is_now(dt_val) or is_now(t_val) or is_now(d_val):  # NOW anywhere wins, even with a date filled in
        return None, True
    if clean(dt_val):
        if isinstance(dt_val, datetime):
            return dt_val.replace(second=0, microsecond=0), False
        return parse_datetime_text(clean(dt_val), order), False
    if clean(d_val):
        if not clean(t_val):
            raise ValueError("Call Time is empty")
        return datetime.combine(parse_date(d_val, order), parse_time(t_val)), False
    if clean(t_val):
        if isinstance(t_val, datetime) and t_val.year > 1900:
            return t_val.replace(second=0, microsecond=0), False
        try:  # old format: full date+time in the time column
            return parse_datetime_text(clean(t_val), order), False
        except ValueError:
            raise ValueError("Call Date is empty") from None
    if settings.blank_time == "reject":
        raise ValueError("No call date/time given")
    return None, True


def validate(sheet: SheetData, received_utc: Optional[datetime] = None, date_order: str = "dmy") -> list[RowCheck]:
    """date_order: how numeric dates are written when the file itself doesn't show it (detect_date_order):
    "dmy" (DD-MM-YYYY, the template) or "mdy" (MM/DD/YYYY). Raises SheetError for a file using both."""
    date_order = detect_date_order(sheet, date_order)
    received_utc = received_utc or utcnow()
    now = utcnow()
    checks: list[RowCheck] = []
    seen_slots: dict[tuple, int] = {}
    seen_ids: dict[str, int] = {}

    for excel_row, cells in sheet.rows:
        data = {fld: clean(cells[idx]) for fld, idx in sheet.mapping.items() if idx < len(cells)}
        chk = RowCheck(excel_row=excel_row, data=data, raw=cells)

        for fld, title in REQUIRED.items():
            if not data.get(fld):  # named as in the client's own file ("Reason for Calling is empty")
                chk.errors.append(f"{sheet.headers[sheet.mapping[fld]] or title if fld in sheet.mapping else title} is empty")
        phone = normalize_phone(cells[sheet.mapping["phone_number"]]) if "phone_number" in sheet.mapping else None
        if data.get("phone_number") and not phone:
            chk.errors.append(f"Phone number '{data['phone_number']}' is not a valid mobile number")
        data["phone_e164"] = phone or ""
        data["phone_national"] = phone[3:] if phone and phone.startswith("+91") else (phone or "")
        data["candidate_id"] = data.get("candidate_id") or f"ROW-{excel_row}"
        if data["candidate_id"] in seen_ids:
            chk.warnings.append(f"Candidate ID repeats row {seen_ids[data['candidate_id']]}")
        seen_ids.setdefault(data["candidate_id"], excel_row)
        if "whatsapp_message_date" in sheet.mapping:
            data["whatsapp_message_date"] = normalize_message_date(cells[sheet.mapping["whatsapp_message_date"]],
                                                                   date_order)

        lang = data.get("initial_language_name", "")
        if lang:
            fixed = next((l for l in SARVAM_LANGUAGES if l.lower() == lang.lower()), None)
            if not fixed:
                chk.warnings.append(f"Language '{lang}' isn't supported; the agent's default is used")
            data["initial_language_name"] = fixed or ""

        try:
            local, call_now = _schedule(sheet, cells, received_utc, date_order)
        except ValueError as exc:
            chk.errors.append(str(exc))
            local, call_now = None, False

        if not chk.errors:
            if call_now:
                sched_utc = now
                chk.slot = "Immediate"
            else:
                sched_utc = local_to_utc(local)
                chk.slot = local.strftime("%Y-%m-%d %H:%M")
                if sched_utc < now - timedelta(hours=settings.past_time_grace_hours):
                    chk.errors.append(f"Call time {local:%d-%m-%Y %H:%M} is in the past. Check the date")
                elif sched_utc < now - timedelta(minutes=5):
                    chk.warnings.append("Call time had already passed; called right away")
            if not chk.errors:
                sched_utc = max(sched_utc, now)
                chk.scheduled_local = local or to_local(now).replace(tzinfo=None)
                chk.scheduled_utc = sched_utc
                chk.effective_utc = next_allowed_time(sched_utc)
                if chk.effective_utc != sched_utc:
                    chk.warnings.append(f"Outside calling hours; moved to {fmt_local(chk.effective_utc)}")
                key = (phone, chk.slot)
                if key in seen_slots:
                    chk.errors.append(f"Same phone number and call time as row {seen_slots[key]}")
                else:
                    seen_slots[key] = excel_row
        checks.append(chk)
    return checks
