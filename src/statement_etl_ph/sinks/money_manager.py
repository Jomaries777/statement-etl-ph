"""A bulk-upload CSV for the Money Manager app.

Transaction -> :class:`MoneyManagerConverter` -> CSV file, with the columns
Money Manager's import expects, in its order::

    Date, Account, Category, Subcategory, Note, Amount, Income/Expense, Description

* **Date** is the transaction's date as the statement prints it.
* **Account** is the bank or e-wallet's name, so it can match an account
  already in the app.
* **Category, Subcategory and Note** are left blank: this package does not
  categorise, and Money Manager lets you sort them after the import.
* **Amount** is always positive, as a plain decimal (``1250.00``). The
  direction is the next column's job.
* **Income/Expense** is ``Income`` for money in and ``Expense`` for money out.
* **Description** is the merchant and the bank's reference number, so every
  row in the app can be traced back to its statement line.

Money Manager has no duplicate check of its own, so importing the same file
twice imports every row twice. One file is written per statement, named
after its bank and period, which makes a second import easy to notice.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import Transaction
from . import SendResult

COLUMNS = (
    "Date",
    "Account",
    "Category",
    "Subcategory",
    "Note",
    "Amount",
    "Income/Expense",
    "Description",
)

INCOME = "Income"
EXPENSE = "Expense"
DEFAULT_DATE_FORMAT = "%Y-%m-%d"


class MoneyManagerConverter:
    """Pure: turns transactions into Money Manager rows, touching no file."""

    def __init__(self, date_format: str = DEFAULT_DATE_FORMAT):
        self.date_format = date_format

    @staticmethod
    def description(txn: Transaction, account: str) -> str:
        """Merchant plus reference: ``Example Apps (ref 0000100000066)``.

        An inbound bank transfer can print no description at all, which
        leaves no merchant; the row is still real, so it gets a name anyway.
        """
        name = txn.merchant or " ".join(txn.description.split()) or f"{account} transaction"
        return f"{name} (ref {txn.reference_no})" if txn.reference_no else name

    def row(self, txn: Transaction, account: str) -> dict[str, str] | None:
        """One import row, or ``None`` for a row that moved no money."""
        if txn.credit:
            kind, amount = INCOME, txn.credit
        elif txn.debit:
            kind, amount = EXPENSE, txn.debit
        else:
            return None
        return {
            "Date": txn.posted_at.strftime(self.date_format),
            "Account": account,
            "Category": "",
            "Subcategory": "",
            "Note": "",
            "Amount": f"{abs(amount):.2f}",
            "Income/Expense": kind,
            "Description": self.description(txn, account),
        }


class MoneyManagerSink:
    def __init__(
        self,
        path: str | Path,
        *,
        date_format: str = DEFAULT_DATE_FORMAT,
        dry_run: bool = False,
    ):
        self.path = Path(path)
        self.converter = MoneyManagerConverter(date_format)
        self.dry_run = dry_run

    def send(self, account: str, txns: list[Transaction]) -> SendResult:
        result = SendResult(location=str(self.path))
        rows = []
        for txn in txns:
            row = self.converter.row(txn, account)
            if row is None:
                result.skipped.append((txn.duplicate_key, "no debit or credit amount"))
                continue
            rows.append(row)
            result.created.append(txn.duplicate_key)

        if self.dry_run or not rows:
            return result

        # Written whole, not appended: one file is one statement, so writing
        # it again replaces it rather than doubling it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return result
