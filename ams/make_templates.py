"""
Rebuild the client templates from CLIENT_FORMAT in ams/sheet.py:

    python -m ams.make_templates

templates/AMS_Calling_Template.csv   header row only (UTF-8 with BOM, so Excel shows Hindi names correctly)
templates/AMS_Calling_Template.xlsx  same columns, cells formatted as text, plus a "How to fill" sheet

Neither file contains a candidate row, so nothing in a template can ever be called by mistake.
"""
from __future__ import annotations

import csv

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import BASE_DIR
from .sheet import CLIENT_FORMAT, TEMPLATE_HEADERS

OUT = BASE_DIR / "templates"
HEAD_FILL = PatternFill("solid", start_color="1F3A5F")
ROWS = 1000  # pre-formatted rows in the Excel template


def write_csv() -> None:
    with open(OUT / "AMS_Calling_Template.csv", "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerow(TEMPLATE_HEADERS)


def write_xlsx() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append(TEMPLATE_HEADERS)
    for col, name in enumerate(TEMPLATE_HEADERS, start=1):
        head = ws.cell(1, col)
        head.font = Font(name="Arial", bold=True, color="FFFFFF")
        head.fill = HEAD_FILL
        head.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = 34 if name == "Pending Documents" else 22
        for row in range(2, ROWS + 2):  # text cells: Excel keeps 9876543210 and 20-11-2026 exactly as typed
            ws.cell(row, col).number_format = "@"
    ws.freeze_panes = "A2"

    guide = wb.create_sheet("How to fill")
    guide.append(["Fill the Candidates sheet: one candidate per row, starting at row 2. "
                  "Keep the column names as they are, then email the file to the AMS calling address."])
    guide.append([])
    guide.append(["Column", "Required", "What to write", "Example"])
    for column, required, rule, example in CLIENT_FORMAT:
        guide.append([column, required, rule, example])
    guide.append([])
    guide.append(["How results come back"])
    guide.append(["You get a reply in the same email thread when the file is received, an updated file each time "
                  "a call time (Call Date + Call Time) finishes, and a FINAL file when every call is done. "
                  "Your columns stay as sent; result columns are added on the right."])
    for cell in guide[3]:
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
    for row in (1, 9):
        guide.cell(row, 1).font = Font(name="Arial", bold=True)
    for col, width in zip("ABCD", (24, 11, 70, 32)):
        guide.column_dimensions[col].width = width
    for row in guide.iter_rows(min_row=4, max_row=3 + len(CLIENT_FORMAT)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(OUT / "AMS_Calling_Template.xlsx")


if __name__ == "__main__":
    write_csv()
    write_xlsx()
    print("Wrote", OUT / "AMS_Calling_Template.csv", "and", OUT / "AMS_Calling_Template.xlsx")
    print("Columns:", ", ".join(TEMPLATE_HEADERS))
