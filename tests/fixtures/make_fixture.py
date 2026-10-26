#!/usr/bin/env python3
"""Generate a synthetic, password-protected GCash-style statement for tests.

Why synthetic: a real statement is private, so it should not live in git. It
is also incomplete as a test — a normal week of GCash spending contains no
credit rows at all, which would leave the inflow path (positive milliunits,
the Credit column) entirely unverified. This fixture deliberately includes
credits, a zero balance, a comma-grouped thousands amount, and a merchant
with a trailing transaction-number suffix.

It reproduces the layout that matters to the parser: Debit, Credit and
Balance figures right-aligned at the same x coordinate as their headers.

    python tests/fixtures/make_fixture.py
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent
OUTPUT = HERE / "synthetic_statement.pdf"
PASSWORD = "testuser0000"

# Column right edges, mirroring a real GCash statement.
X_DATE = 50
X_DESC = 126
X_REF_RIGHT = 362
X_DEBIT_RIGHT = 435
X_CREDIT_RIGHT = 486
X_BALANCE_RIGHT = 538

# Every merchant, name and reference number is invented. The dates, amounts
# and balances are what the tests and the printed totals below depend on.
ROWS = [
    # (date, time, description, ref, debit, credit, balance)
    ("2026-08-22", "12:31 PM", "Payment to Sample Mart Example St Branch", "0000100000011", "216.00", "", "3787.68"),
    ("2026-08-23", "01:28 PM", "Payment to Example Noodle House", "0000100000022", "385.00", "", "3402.68"),
    ("2026-08-24", "06:21 AM", "Payment to Sample Rides", "0000100000033", "59.00", "", "3343.68"),
    # An inflow: the Credit column, which real weekly statements often lack.
    ("2026-08-24", "09:00 AM", "Receive Money from TEST U", "0000100000044", "", "1,500.00", "4843.68"),
    ("2026-08-25", "07:25 AM", "Payment to Sample Grocer Inc", "0000100000055", "31.50", "", "4812.18"),
    # A merchant carrying a per-transaction suffix that must be stripped.
    ("2026-08-26", "09:09 AM", "Payment to Example Apps, Merchant Transaction Number: EXMPL00000A1", "0000100000066", "489.00", "", "4323.18"),
    # GCash sometimes prints 0.00 in the balance column.
    ("2026-08-26", "03:26 PM", "Payment to Sample Kitchen", "0000100000077", "75.00", "", "0.00"),
    # A thousands separator on the debit side.
    ("2026-08-27", "07:30 PM", "Payment to Example Power Co", "0000100000088", "1,250.00", "", "2998.18"),
]

STARTING_BALANCE = "4003.68"
ENDING_BALANCE = "2998.18"
TOTAL_DEBIT = "2505.50"   # 216 + 385 + 59 + 31.50 + 489 + 75 + 1250
TOTAL_CREDIT = "1500.00"


def build(
    output: Path = OUTPUT,
    rows=ROWS,
    *,
    starting_balance: str = STARTING_BALANCE,
    ending_balance: str = ENDING_BALANCE,
    total_debit: str = TOTAL_DEBIT,
    total_credit: str = TOTAL_CREDIT,
) -> Path:
    """Write the statement. Tests build variants by passing their own rows."""
    output = Path(output)
    unencrypted = output.with_name(f"_plain_{output.name}")
    pdf = canvas.Canvas(str(unencrypted), pagesize=A4)
    width, height = A4

    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(width / 2, height - 40, "GCash Transaction History")
    pdf.setFont("Helvetica", 9)
    pdf.drawCentredString(width / 2, height - 74, "2026-08-22 to 2026-08-28")

    y = height - 98
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(X_DATE, y, "Date and Time")
    pdf.drawString(208, y, "Description")
    pdf.drawString(335, y, "Reference No.")
    pdf.drawRightString(X_DEBIT_RIGHT, y, "Debit")
    pdf.drawRightString(X_CREDIT_RIGHT, y, "Credit")
    pdf.drawRightString(X_BALANCE_RIGHT, y, "Balance")

    pdf.setFont("Helvetica", 7)
    y -= 11
    pdf.drawString(X_DESC, y, "STARTING BALANCE")
    pdf.drawRightString(X_BALANCE_RIGHT, y, starting_balance)

    # Real statements shrink a long description so it stops short of the
    # reference column rather than colliding with it. Mimic that, otherwise
    # the glyphs interleave and the fixture tests a failure mode that the
    # actual GCash renderer never produces. The limit is measured from where
    # this row's reference actually starts, plus a gap pdfplumber will not
    # bridge: a fixed allowance once let a description's last digit merge
    # into a 13-digit reference.

    for date, time, description, ref, debit, credit, balance in rows:
        y -= 11
        pdf.drawString(X_DATE, y, f"{date} {time}")
        size = 7.0
        desc_limit = X_REF_RIGHT - pdf.stringWidth(ref, "Helvetica", 7) - 6 - X_DESC
        while size > 4.0 and pdf.stringWidth(description, "Helvetica", size) > desc_limit:
            size -= 0.25
        pdf.setFont("Helvetica", size)
        pdf.drawString(X_DESC, y, description)
        pdf.setFont("Helvetica", 7)
        pdf.drawRightString(X_REF_RIGHT, y, ref)
        if debit:
            pdf.drawRightString(X_DEBIT_RIGHT, y, debit)
        if credit:
            pdf.drawRightString(X_CREDIT_RIGHT, y, credit)
        pdf.drawRightString(X_BALANCE_RIGHT, y, balance)

    y -= 11
    pdf.drawString(X_DESC, y, "ENDING BALANCE")
    pdf.drawRightString(X_BALANCE_RIGHT, y, ending_balance)
    y -= 11
    pdf.drawString(X_DESC, y, "Total Debit")
    pdf.drawRightString(X_DEBIT_RIGHT, y, total_debit)
    y -= 11
    pdf.drawString(X_DESC, y, "Total Credit")
    pdf.drawRightString(X_CREDIT_RIGHT, y, total_credit)

    pdf.save()

    with pikepdf.open(unencrypted) as source:
        source.save(
            output,
            encryption=pikepdf.Encryption(owner=PASSWORD, user=PASSWORD, R=4),
        )
    unencrypted.unlink()
    # The password is synthetic, but nothing here prints a password: see PASSWORD above.
    print(f"wrote {output} (encrypted with PASSWORD from this script)")
    return output


if __name__ == "__main__":
    build()
