"""A plain CSV of the statement, for a spreadsheet or any tracker without an API.

One row per transaction, amounts as plain decimals, and a ``duplicate_key``
column built from the bank's own reference number. Writing to a file that
already exists appends only the rows whose key is not in it yet, so fetching
the same statement twice into one file does not double it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import Transaction
from . import SendResult

COLUMNS = (
    "date",
    "time",
    "account",
    "merchant",
    "description",
    "amount",
    "debit",
    "credit",
    "balance",
    "reference_no",
    "duplicate_key",
)


def _money(value) -> str:
    return "" if value is None else f"{value:.2f}"


class CsvSink:
    def __init__(self, path: str | Path, *, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run

    def _existing_keys(self) -> set[str]:
        if not self.path.exists():
            return set()
        with self.path.open(newline="", encoding="utf-8") as handle:
            return {row.get("duplicate_key", "") for row in csv.DictReader(handle)}

    def send(self, account: str, txns: list[Transaction]) -> SendResult:
        result = SendResult(location=str(self.path))
        seen = self._existing_keys()
        rows = []
        for txn in txns:
            key = txn.duplicate_key
            if key in seen:
                result.duplicates.append(key)
                continue
            seen.add(key)
            rows.append(
                {
                    "date": txn.value_date.isoformat(),
                    "time": txn.posted_at.strftime("%H:%M"),
                    "account": account,
                    "merchant": txn.merchant,
                    "description": txn.description,
                    "amount": _money(txn.signed_amount),
                    "debit": _money(txn.debit),
                    "credit": _money(txn.credit),
                    "balance": _money(txn.balance),
                    "reference_no": txn.reference_no,
                    "duplicate_key": key,
                }
            )
            result.created.append(key)

        if self.dry_run or not rows:
            return result

        new_file = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        return result
