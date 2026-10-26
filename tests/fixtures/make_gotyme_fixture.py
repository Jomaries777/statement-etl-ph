#!/usr/bin/env python3
"""Generate a synthetic, password-protected GoTyme-style statement for tests.

Synthetic for the same reasons as the GCash fixture — a real statement is
private and should not be committed — plus one specific to this bank. The
GoTyme layout has three features a typical week does not exercise together,
and each one silently corrupts the import if the parser gets it wrong:

* **Two identical payments on one day.** GoTyme prints no reference numbers,
  so these are distinguishable only by the running balance. Without that,
  both collapse to one import ID and YNAB imports a single transaction.
* **A description wrapped around its own data line** — text above the date
  and continuing below it, as GoTyme does for long transfer descriptions.
* **A ``0.00`` in the unused money column**, which must read as "not this
  side" rather than a credit of zero.

It also spans two pages, because the column headers repeat per page and a
parser that reads only the first page loses everything after it.

The masking characters are asterisks rather than the bullets GoTyme really
prints: reportlab's Helvetica has no glyph for U+2022 and pdfplumber reads it
back as ``(cid:127)``, which would test an artefact of the generator instead
of the layout. The real bullet is covered directly in the merchant tests.

    python tests/fixtures/make_gotyme_fixture.py
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent
OUTPUT = HERE / "synthetic_gotyme_statement.pdf"
# Deliberately not anyone's real date of birth; GoTyme's format is MMDDYYYY.
PASSWORD = "01011990"

# Mirrors the real statement's geometry: the money columns are right-aligned
# and the Details column starts well right of the Date column.
X_DATE = 18.8
X_DETAILS = 71.2
X_CREDIT_RIGHT = 351.2
X_DEBIT_RIGHT = 388.1
X_BALANCE_RIGHT = 440.3
ROW_GAP = 15.7
WRAP_GAP = 3.9

OPENING = "1,000.00"
CLOSING = "1,316.44"
TOTAL_CREDIT = "2,399.00"
TOTAL_DEBIT = "2,082.56"

# (date, description, wrapped_tail, credit, debit, balance)
# wrapped_tail is printed BELOW the data line, the description ABOVE it.
PAGE_ONE = [
    ("01 Aug 2026", "Card payment at COFFEE PLACE, Card **** 1234", None, "0.00", "150.00", "850.00"),
    ("02 Aug 2026", "Card payment at RIDEHAIL, Card **** 1234", None, "0.00", "99.00", "751.00"),
    # Same day, same payee, same amount: only the balance separates them.
    ("02 Aug 2026", "Card payment at RIDEHAIL, Card **** 1234", None, "0.00", "99.00", "652.00"),
    ("03 Aug 2026", "Refund for card payment", None, "99.00", "0.00", "751.00"),
    # An internal pocket move, which the gotyme source excludes from the load.
    ("04 Aug 2026", "Transfer to GoalSave", None, "0.00", "500.00", "251.00"),
]
PAGE_TWO = [
    ("05 Aug 2026", "Transfer from GoalSave", None, "300.00", "0.00", "551.00"),
    # The wrapped case: description above the data line, account number below.
    (
        "06 Aug 2026",
        "Transfer from J. D. SANTOS Example Bank, Inc, Account No.",
        "********9999",
        "2,000.00",
        "0.00",
        "2,551.00",
    ),
    ("07 Aug 2026", "Card payment at BIG SUPERMARKET, Card **** 5678", None, "0.00", "1,234.56", "1,316.44"),
]


def _headers(pdf, width, height) -> float:
    pdf.setFont("Helvetica", 8)
    pdf.drawString(305.4, height - 21.2, "22nd Floor, GBF Center 1, Bridgetowne, C-5 Road,")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(15.0, height - 77.3, "Statement of Account")
    pdf.setFont("Helvetica", 7)
    pdf.drawString(414.6, height - 84.7, "Doc ID:")
    pdf.drawString(404.8, height - 92.2, "TESTDOC12345")

    # The last column header wraps as "Running" over "Balance", which is why
    # the parser clusters lines vertically before looking for the headers.
    y = height - 107.6
    pdf.drawString(417.1, y, "Running")
    pdf.drawString(X_DATE, y - WRAP_GAP, "Date")
    pdf.drawString(X_DETAILS, y - WRAP_GAP, "Details")
    pdf.drawRightString(X_CREDIT_RIGHT, y - WRAP_GAP, "Credits")
    pdf.drawRightString(X_DEBIT_RIGHT, y - WRAP_GAP, "Debits")
    pdf.drawString(417.8, y - 2 * WRAP_GAP, "Balance")
    return y - 2 * WRAP_GAP - 17.0


def _rows(pdf, y: float, rows) -> None:
    pdf.setFont("Helvetica", 7)
    for when, description, tail, credit, debit, balance in rows:
        if tail is None:
            pdf.drawString(X_DETAILS, y, description)
        else:
            # Wrapped: description above the data line, tail below it.
            pdf.drawString(X_DETAILS, y + WRAP_GAP, description)
            pdf.drawString(X_DETAILS, y - WRAP_GAP, tail)
        pdf.drawString(X_DATE, y, when)
        pdf.drawRightString(X_CREDIT_RIGHT, y, credit)
        pdf.drawRightString(X_DEBIT_RIGHT, y, debit)
        pdf.drawRightString(X_BALANCE_RIGHT, y, balance)
        y -= ROW_GAP


def build() -> Path:
    unencrypted = HERE / "_gotyme_plain.pdf"
    pdf = canvas.Canvas(str(unencrypted), pagesize=A4)
    width, height = A4

    y = _headers(pdf, width, height)
    pdf.setFont("Helvetica", 7)
    # The summary block, which the parser reads for reconciliation.
    pdf.drawString(200.0, height - 130.0, f"Opening balance PHP {OPENING}")
    pdf.drawString(200.0, height - 140.0, f"Total Credit PHP {TOTAL_CREDIT}")
    pdf.drawString(200.0, height - 150.0, f"Total Debit PHP {TOTAL_DEBIT}")
    pdf.drawString(200.0, height - 160.0, f"Closing balance PHP {CLOSING}")
    pdf.drawString(15.0, height - 175.0, "Account Number: 000000000000")
    pdf.drawString(15.0, height - 185.0, "Period: 01 Aug 2026 - 07 Aug 2026")
    _rows(pdf, y - 90.0, PAGE_ONE)
    pdf.drawString(15.0, 60.0, "All figures are in PHP.")
    pdf.showPage()

    y = _headers(pdf, width, height)
    _rows(pdf, y, PAGE_TWO)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(15.0, 60.0, "All figures are in PHP.")
    pdf.drawString(15.0, 50.0, "Document ID: TESTDOC12345")
    pdf.save()

    with pikepdf.open(unencrypted) as source:
        source.save(
            OUTPUT,
            encryption=pikepdf.Encryption(owner=PASSWORD, user=PASSWORD, R=4),
        )
    unencrypted.unlink()
    # The password is synthetic, but nothing here prints a password: see PASSWORD above.
    print(f"wrote {OUTPUT} (encrypted with PASSWORD from this script)")
    return OUTPUT


if __name__ == "__main__":
    build()
