"""Shared geometry for column-based statement PDFs.

The PDF parsers read a statement as a grid rather than as text: find where the
money columns sit by locating their header row, then assign each numeric
token on every row to the nearest column by its right edge. A text-only
parse of e.g. ``59.00   3343.68`` cannot tell a debit from a credit, and
getting that backwards flips the sign of a transaction.

What is shared here is the technique. What is not is the vocabulary: which
headers exist, how forgiving the column and row tolerances need to be, and
how words within a row should read back in order. All three come in as
parameters, never as constants of this module — verified empirically
(replaying each provider's statements through the other's tolerances and
diffing the result), because sharing more than this turned out not to be
safe. Row clustering in particular looks
identical at a glance but is not: an anchor-per-bucket approach and a
sorted-sequential one only coincide when rows never wrap, which held for
both statements tested but is not guaranteed by the code shape alone —
hence the ``sort_key`` and ``tolerance`` parameters stay explicit rather
than becoming module constants.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Optional

from .models import StatementParseError

logger = logging.getLogger(__name__)

# Identical in both providers' statements: 1,234.56 / 216.00 / -0.00
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")


def to_decimal(token: str) -> Optional[Decimal]:
    try:
        return Decimal(token.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def cluster_rows(
    words: Iterable[dict],
    tolerance: float,
    sort_key: Callable[[dict], tuple] = lambda w: (w["x0"],),
) -> list[list[dict]]:
    """Group words into logical rows by vertical position.

    Words are merged into the same row while the gap to the previous one
    stays within ``tolerance`` — walking the distinct ``top`` values in
    order rather than anchoring each row to its first word lets a wrapped
    description spanning more than one physical line join the data line
    between them into a single logical row.

    ``sort_key`` fixes read order within a row. A row that never wraps
    reads correctly sorted by ``x0`` alone; one that can wrap needs
    ``(top, x0)`` so a line above or below the data line lands before or
    after it rather than interleaving by horizontal position.
    """
    by_top: dict[float, list[dict]] = defaultdict(list)
    for word in words:
        by_top[round(word["top"], 1)].append(word)

    rows: list[list[dict]] = []
    current: list[dict] = []
    previous: Optional[float] = None
    for top in sorted(by_top):
        if previous is not None and top - previous > tolerance:
            rows.append(current)
            current = []
        current.extend(by_top[top])
        previous = top
    if current:
        rows.append(current)
    return [sorted(row, key=sort_key) for row in rows]


def find_money_columns(rows: list[list[dict]], headers: tuple[str, ...]) -> dict[str, float]:
    """Locate the right edge of each money column from its header row."""
    for row in rows:
        labels = {w["text"].strip().lower(): w for w in row}
        if all(header in labels for header in headers):
            return {header: labels[header]["x1"] for header in headers}
    names = "/".join(h.title() for h in headers)
    raise StatementParseError(
        f"could not find the {names} header row; the statement layout may have changed"
    )


def assign_money_columns(
    row: list[dict], columns: dict[str, float], tolerance: float
) -> dict[str, Decimal]:
    """Map each money-looking token on a row to its nearest column, by right edge."""
    assigned: dict[str, Decimal] = {}
    for word in row:
        token = word["text"].strip()
        if not AMOUNT_RE.match(token):
            continue
        value = to_decimal(token)
        if value is None:
            continue
        best_col, best_dist = None, float("inf")
        for name, edge in columns.items():
            dist = abs(word["x1"] - edge)
            if dist < best_dist:
                best_col, best_dist = name, dist
        if best_col is None or best_dist > tolerance:
            logger.warning(
                "amount %s at x1=%.1f matched no money column (nearest %s, %.1fpt away)",
                token,
                word["x1"],
                best_col,
                best_dist,
            )
            continue
        # A row should never carry two figures in one column; keep the first
        # and warn rather than let a later, possibly spurious match silently
        # overwrite it.
        if best_col in assigned:
            logger.warning("two values landed in the %s column on one row", best_col)
            continue
        assigned[best_col] = value
    return assigned
