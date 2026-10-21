"""GoTyme Bank: decrypt a statement of account and extract its table.

The technique is the one :mod:`statement_etl_ph.banks.gcash` uses, from
:mod:`statement_etl_ph.pdf_geometry`: find the money column
headers, then assign each numeric token to a column by its right edge. What
does not transfer is the vocabulary, and three differences are load-bearing.

**Both money columns are always printed.** GoTyme writes ``0.00`` in the
column a row does not use, so a row reading ``0.00  174.25`` is a debit of
174.25 and *not* a credit of zero. A zero is therefore read as "not this
side" and stored as ``None``; storing ``Decimal("0")`` would make
``signed_amount`` return zero for every transaction on the statement.

**Descriptions wrap around the data line.** A long payee is printed on the
line *above* the date and amounts, and continues on the line *below* them:

    Transfer from J. D. SANTOS Maya Philippines, Inc (PayMaya Wallet), ...
    08 Aug 2026                                    3,000.00   0.00  3,445.95
    ••••••••0000

Those three lines are one logical row. Geometry separates them from ordinary
rows without guesswork: wrapped lines sit 3.9pt apart and start in the
Details column, while consecutive rows are 15.7pt apart and start in the Date
column. So lines are clustered vertically with a threshold between the two,
and the description is every non-date, non-amount word in the cluster read
top-to-bottom, left-to-right.

**There are no reference numbers.** GCash prints one per row, which makes an
ideal idempotency key. GoTyme prints none, and the usual fallback of
``amount:date`` is not good enough here: this statement contains two GRAB
payments of 149.00 on the same day, which would collide and silently import
as one. The running balance disambiguates them — it is a ledger, so no two
rows share one — so each row gets a synthetic reference derived from date,
description, both amounts and that balance. It is stable across re-runs of
the same statement, which is what idempotency needs.

What it is *not* stable across is a **reissued** statement. A backdated
adjustment shifts every running balance after it, so every row from that
point onward gets a new key and re-imports alongside the originals. That is
accepted rather than solved: dropping the balance trades visible duplicates
for silent data loss, keying on row position breaks on any insertion, and
hashing date and description alone collides on genuinely repeated purchases —
one real month held eight identical ``MOVE IT`` rows. Duplicates you can see
and delete beat a transaction that never arrives.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pdfplumber

from ..models import Statement, StatementParseError, Transaction
from .base import BankDefinition
from ..pdf_geometry import (
    AMOUNT_RE,
    assign_money_columns,
    cluster_rows,
    find_money_columns,
    to_decimal,
)

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^(\d{1,2}) ([A-Za-z]{3}) (\d{4})$")
_PERIOD_RE = re.compile(
    r"Period:\s*(\d{1,2} [A-Za-z]{3} \d{4})\s*[-–]\s*(\d{1,2} [A-Za-z]{3} \d{4})"
)
_SUMMARY_RES = {
    "starting_balance": re.compile(r"Opening balance\s+[A-Z]{3}\s+([\d,]+\.\d{2})"),
    "ending_balance": re.compile(r"Closing balance\s+[A-Z]{3}\s+([\d,]+\.\d{2})"),
    "total_credit": re.compile(r"Total Credit\s+[A-Z]{3}\s+([\d,]+\.\d{2})"),
    "total_debit": re.compile(r"Total Debit\s+[A-Z]{3}\s+([\d,]+\.\d{2})"),
}

# Wrapped continuation lines sit 3.9pt from their data line; separate rows sit
# 15.7pt apart. Anything between the two separates rows without splitting one.
_ROW_CLUSTER_TOLERANCE = 10.0
# Credits and Debits are ~37pt apart, so half that is a safe column tolerance.
_COLUMN_TOLERANCE = 15.0
_MONEY_HEADERS = ("credits", "debits", "balance")


def _cluster_rows(words) -> list[list[dict]]:
    """Group words into logical rows, keeping a wrapped description together.

    A row can wrap above and below its data line, so words must read back
    top-to-bottom before left-to-right, or a wrapped line would interleave
    with the data line by horizontal position instead of following it.
    """
    return cluster_rows(words, _ROW_CLUSTER_TOLERANCE, sort_key=lambda w: (w["top"], w["x0"]))


def _synthetic_reference(
    when: date,
    description: str,
    credit: Optional[Decimal],
    debit: Optional[Decimal],
    balance: Optional[Decimal],
) -> str:
    """A stable per-row key, standing in for the reference GoTyme omits.

    Includes the running balance deliberately: it is what distinguishes two
    otherwise identical payments on the same day.
    """
    payload = "|".join(
        [
            when.isoformat(),
            " ".join(description.split()).lower(),
            str(credit if credit is not None else ""),
            str(debit if debit is not None else ""),
            str(balance if balance is not None else ""),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_row(
    row: list[dict], columns: dict[str, float]
) -> Optional[Transaction]:
    amounts = [w for w in row if AMOUNT_RE.match(w["text"])]
    if len(amounts) < 3:
        return None

    # The date is three consecutive words ("08", "Aug", "2026") in the Date
    # column. Find them rather than assuming they start the cluster — on a
    # wrapped row the description is printed above them.
    texts = [w["text"] for w in row]
    when: Optional[date] = None
    date_indices: set[int] = set()
    for i in range(len(texts) - 2):
        candidate = " ".join(texts[i : i + 3])
        if _DATE_RE.match(candidate):
            try:
                when = datetime.strptime(candidate, "%d %b %Y").date()
            except ValueError:
                continue
            date_indices = {i, i + 1, i + 2}
            break
    if when is None:
        return None

    values = assign_money_columns(row, columns, _COLUMN_TOLERANCE)
    credit = values.get("credits")
    debit = values.get("debits")
    balance = values.get("balance")

    # GoTyme prints 0.00 in the column a row does not use. Treating that as a
    # real zero would make every transaction worth nothing.
    if credit == 0:
        credit = None
    if debit == 0:
        debit = None
    if credit is None and debit is None:
        return None

    amount_ids = {id(w) for w in amounts}
    description = " ".join(
        w["text"]
        for i, w in enumerate(row)
        if i not in date_indices and id(w) not in amount_ids
    )
    description = " ".join(description.split())
    if not description:
        return None

    return Transaction(
        posted_at=datetime(when.year, when.month, when.day),
        description=description,
        reference_no=_synthetic_reference(when, description, credit, debit, balance),
        debit=debit,
        credit=credit,
        balance=balance,
    )


def _parse_summary(text: str) -> dict[str, Optional[Decimal]]:
    found: dict[str, Optional[Decimal]] = {}
    for field, pattern in _SUMMARY_RES.items():
        match = pattern.search(text)
        found[field] = to_decimal(match.group(1)) if match else None
    return found


def _parse_period(text: str) -> tuple[Optional[date], Optional[date]]:
    match = _PERIOD_RE.search(text)
    if not match:
        return None, None
    try:
        start = datetime.strptime(match.group(1), "%d %b %Y").date()
        end = datetime.strptime(match.group(2), "%d %b %Y").date()
    except ValueError:
        return None, None
    return start, end


def parse_statement(pdf_path: str | Path, password: str) -> Statement:
    """Parse a password-protected GoTyme statement into a :class:`Statement`."""
    transactions: list[Transaction] = []
    full_text: list[str] = []

    with pdfplumber.open(str(pdf_path), password=password) as pdf:
        all_rows: list[list[dict]] = []
        for page in pdf.pages:
            full_text.append(page.extract_text() or "")
            all_rows.extend(_cluster_rows(page.extract_words()))

        # GoTyme wraps the last header as "Running" over "Balance", so the
        # three labels only land in one row because rows are grouped
        # vertically first.
        columns = find_money_columns(all_rows, _MONEY_HEADERS)
        logger.debug("money columns at %s", columns)
        for row in all_rows:
            transaction = _parse_row(row, columns)
            if transaction is not None:
                transactions.append(transaction)

    if not transactions:
        raise StatementParseError(
            "no transaction rows found; the statement may be empty or its "
            "layout may have changed"
        )

    text = "\n".join(full_text)
    start, end = _parse_period(text)
    summary = _parse_summary(text)

    logger.info(
        "parsed %d transactions for %s to %s",
        len(transactions),
        start or "?",
        end or "?",
    )
    return Statement(
        period_start=start,
        period_end=end,
        starting_balance=summary["starting_balance"],
        ending_balance=summary["ending_balance"],
        total_debit=summary["total_debit"],
        total_credit=summary["total_credit"],
        transactions=transactions,
    )


BANK = BankDefinition(
    name="gotyme",
    display_name="GoTyme",
    parser=parse_statement,
    gmail_query='from:no-reply@gotyme.com.ph subject:"Statement of Account" has:attachment',
    # Not amazonses.com, though GoTyme sends through it: SES signs for anyone.
    dkim_domains=("gotyme.com.ph",),
    password_env="GOTYME_PDF_PASSWORD",
    # One email carries the deposit and the savings statements. The config
    # file can pick the other one.
    attachment_patterns=("EverydayDepositAccount",),
)
