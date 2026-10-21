"""GCash: decrypt the emailed transaction-history PDF and extract its table.

GCash renders the statement as a borderless table, so ``extract_table()``
guesses badly at it. What *is* reliable is geometry: the Debit, Credit and
Balance figures are right-aligned under their headers, always at the same x
coordinate. So we read the header row once to learn where the money columns
live, then assign every numeric token on every row to the nearest column.

That distinction matters. A text-only parse of ``59.00   3343.68`` cannot
tell a debit from a credit, and getting it backwards would flip the sign of
the transaction wherever it ends up.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pdfplumber

from ..models import Statement, StatementParseError, Transaction
from .base import BankDefinition
from ..pdf_geometry import AMOUNT_RE, assign_money_columns, cluster_rows, find_money_columns

logger = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})")
# A GCash reference number: a long digit run, distinct from an amount.
_REFERENCE_RE = re.compile(r"^\d{9,}$")
# Same, but rescued from inside a token that picked up neighbouring glyphs.
_DIGIT_RUN_RE = re.compile(r"\d{9,}")

# Rows are single lines that never wrap, so grouping words a fraction of a
# point apart in top position is enough to call them the same row.
_ROW_CLUSTER_TOLERANCE = 3.0
# How close (in points) a number's right edge must be to a header's right edge
# before we accept the match. The three columns are ~50pt apart, so 25 is a
# safe half-width.
_COLUMN_TOLERANCE = 25.0

_MONEY_HEADERS = ("debit", "credit", "balance")


def _group_words_into_lines(words) -> list[list[dict]]:
    """Cluster extracted words into visual rows, in left-to-right reading order.

    No GCash row wraps, so sorting purely by ``x0`` is always reading order.
    """
    return cluster_rows(words, _ROW_CLUSTER_TOLERANCE, sort_key=lambda w: (w["x0"],))


def _find_reference_zone(lines: list[list[dict]]) -> Optional[tuple[float, float]]:
    """Locate the horizontal span of the 'Reference No.' column.

    Position is a better signal than pattern matching here. A long merchant
    description can contain digit runs of its own, and reference numbers are
    not always the first such run on the row; the column they sit in is the
    thing that actually identifies them.
    """
    for line in lines:
        for index, word in enumerate(line):
            if word["text"].strip().lower() != "reference":
                continue
            right = word["x1"]
            if index + 1 < len(line) and line[index + 1]["text"].strip().lower() == "no.":
                right = line[index + 1]["x1"]
            return (word["x0"] - 30.0, right + 15.0)
    return None


def _split_reference_and_description(
    words: list[dict], zone: Optional[tuple[float, float]]
) -> tuple[str, str]:
    """Separate the reference number from the description on one row."""
    reference = ""
    description_parts: list[str] = []

    for word in words:
        token = word["text"].strip()
        if not token or AMOUNT_RE.match(token):
            continue

        in_zone = zone is not None and word["x1"] >= zone[0] and word["x0"] <= zone[1]
        if not reference and (in_zone or zone is None):
            match = _REFERENCE_RE.match(token) or _DIGIT_RUN_RE.search(token)
            if match:
                reference = match.group(0)
                continue

        # Anything to the right of the reference column is a stray artefact,
        # not part of the merchant name.
        if zone is not None and word["x0"] > zone[1]:
            continue
        if not reference:
            description_parts.append(token)

    return reference, " ".join(description_parts).strip()


def _row_text(line: list[dict]) -> str:
    return " ".join(w["text"] for w in line)


def _parse_datetime(date_token: str, time_token: str) -> Optional[datetime]:
    cleaned = time_token.upper().replace(".", "").replace(" ", "")
    for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date_token} {cleaned}", fmt)
        except ValueError:
            continue
    return None


def parse_statement(pdf_path: str | Path, password: str) -> Statement:
    """Decrypt and parse a GCash statement PDF into a :class:`Statement`."""
    pdf_path = Path(pdf_path)
    transactions: list[Transaction] = []
    starting_balance = ending_balance = total_debit = total_credit = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None

    with pdfplumber.open(str(pdf_path), password=password) as pdf:
        if not pdf.pages:
            raise StatementParseError("PDF has no pages")

        columns: Optional[dict[str, float]] = None
        reference_zone: Optional[tuple[float, float]] = None

        for page_no, page in enumerate(pdf.pages, start=1):
            lines = _group_words_into_lines(page.extract_words(keep_blank_chars=False))

            if columns is None:
                try:
                    columns = find_money_columns(lines, _MONEY_HEADERS)
                    reference_zone = _find_reference_zone(lines)
                    logger.debug(
                        "money columns at %s, reference zone %s", columns, reference_zone
                    )
                except StatementParseError:
                    if page_no == len(pdf.pages):
                        raise
                    continue  # header might be on a later page

            for line in lines:
                text = _row_text(line)

                if period_start is None:
                    period_match = _PERIOD_RE.search(text)
                    if period_match:
                        period_start = date.fromisoformat(period_match.group(1))
                        period_end = date.fromisoformat(period_match.group(2))
                        continue

                amounts = assign_money_columns(line, columns, _COLUMN_TOLERANCE)
                upper = text.upper()

                if "STARTING BALANCE" in upper:
                    starting_balance = amounts.get("balance")
                    continue
                if "ENDING BALANCE" in upper:
                    ending_balance = amounts.get("balance")
                    continue
                if "TOTAL DEBIT" in upper:
                    total_debit = amounts.get("debit")
                    continue
                if "TOTAL CREDIT" in upper:
                    total_credit = amounts.get("credit")
                    continue

                # A transaction row starts with a date and a time.
                if len(line) < 3:
                    continue
                stamp = _parse_datetime(line[0]["text"], f"{line[1]['text']} {line[2]['text']}")
                if stamp is None:
                    continue

                reference, description = _split_reference_and_description(
                    line[3:], reference_zone
                )
                if not description:
                    logger.warning("row at %s has no description; keeping it anyway", stamp)

                if not amounts.get("debit") and not amounts.get("credit"):
                    logger.warning(
                        "skipping row with no debit or credit amount: %s", text[:90]
                    )
                    continue

                transactions.append(
                    Transaction(
                        posted_at=stamp,
                        description=description,
                        reference_no=reference,
                        debit=amounts.get("debit"),
                        credit=amounts.get("credit"),
                        balance=amounts.get("balance"),
                    )
                )

    if not transactions:
        raise StatementParseError("no transaction rows found in the statement")

    statement = Statement(
        period_start=period_start,
        period_end=period_end,
        starting_balance=starting_balance,
        ending_balance=ending_balance,
        total_debit=total_debit,
        total_credit=total_credit,
        transactions=transactions,
    )
    logger.info(
        "parsed %d transactions for %s to %s",
        len(transactions),
        period_start,
        period_end,
    )
    return statement


BANK = BankDefinition(
    name="gcash",
    display_name="GCash",
    parser=parse_statement,
    gmail_query=(
        'from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History" '
        "has:attachment"
    ),
    dkim_domains=("gcash.com",),
    password_env="GCASH_PDF_PASSWORD",
)
