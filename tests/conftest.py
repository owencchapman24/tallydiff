"""Small in-memory workbook fixtures; no binary test files are tracked."""

from io import BytesIO

import pytest
from openpyxl import Workbook


@pytest.fixture
def xlsx_bytes():
    def build(sheets, *, configure=None):
        workbook = Workbook()
        workbook.remove(workbook.active)
        workbook.iso_dates = True
        try:
            for name, rows in sheets.items():
                sheet = workbook.create_sheet(name)
                for row in rows:
                    sheet.append(row)
            if configure is not None:
                configure(workbook)
            with BytesIO() as stream:
                workbook.save(stream)
                return stream.getvalue()
        finally:
            workbook.close()

    return build


@pytest.fixture
def acceptance_workbooks(xlsx_bytes):
    return (
        xlsx_bytes(
            {
                "Ledger A": [
                    ["Vendor ID", "Invoice Number", "Invoice Amount"],
                    ["V001", "1042", 1250],
                    ["V002", "1043", 800],
                    ["V003", "1044", 500],
                ]
            }
        ),
        xlsx_bytes(
            {
                "Ledger B": [
                    ["Supplier", "Invoice Ref", "Gross Amount"],
                    ["V001", "1042", 1205],
                    ["V002", "1043", 800],
                    ["V004", "1045", 300],
                ]
            }
        ),
    )
