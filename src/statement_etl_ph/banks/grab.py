"""Grab: read one "Your Grab E-Receipt" email into a statement.

Grab sends no statement. It sends a receipt per ride, per food order and per
tip, as an HTML email body with no attachment. So the "statement" here is a
single receipt, and the parser reads the body the source fetched instead of a
PDF. It keeps the ``(path, password) -> Statement`` shape every other parser
has, so nothing downstream needs to know the difference; the password is
simply unused.

**Only GrabPay Wallet payments are imported.** A receipt paid by card or by
GCash is already on that account's own statement — the Visa order lands on
the credit card, the GCash ride on the GCash statement this package already
imports. Importing it here as well would count it twice. Such a receipt
parses to a statement with no rows, so the email is still labelled as done.

Three receipt layouts are recognised, told apart by their opening line:

* **food** — "Hope you enjoyed your food!", with ``Payment Method:`` and a
  ``TOTAL (INCL. TAX)`` line;
* **ride** — "Hope you enjoyed your ride!", with a ``Paid by`` block listing
  each method and its amount;
* **tip** — "Your tip goes a long way", also with a ``Paid by`` block. It
  carries the same booking ID as the order it tips, so its reference gets a
  ``-TIP`` suffix; without one a tracker would reject it as a duplicate of the
  order.

Anything else is a parse error rather than a guess. Grab uses this subject for
layouts not seen yet (GrabMart, Express), and a receipt this code cannot read
should be visible in the run report, not dropped or misread.

The receipt's own arithmetic stands in for a statement's printed totals. A
food receipt prints its total twice and both must agree; a ride or tip lists
what each method paid, and those must add up to the total. A receipt that
does not close was misread, and fails exactly the way an unreconciled
statement does.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ..models import Statement, StatementParseError, Transaction
from .base import BankDefinition

WALLET = "GrabPay Wallet"

# Manila. Receipts print +0800 today; converting rather than trusting that
# keeps a receipt stamped in another offset on the right day.
_LOCAL = timezone(timedelta(hours=8))

# Grab prints the peso sign three ways across its templates: "₱ 538.00",
# "₱392.00" and, on older and tip receipts, "P 50.00".
_PESO = r"(?:₱|PHP|P)?\s*"
_AMOUNT = r"([\d,]+\.\d{2})"
# Cell boundaries survive as pipes in some renderings of the same email.
_GAP = r"[\s|]*"

_BOOKING = re.compile(r"Booking (?:code|ID):?" + _GAP + r"(A-[A-Z0-9]+)")


@dataclass(frozen=True)
class Payment:
    method: str
    amount: Decimal


def _flatten(raw: str) -> str:
    """The email as one line of visible text.

    Tags become spaces rather than nothing, so adjacent table cells stay
    separate words; entities are decoded so ``&#8369;`` reads as the peso sign.
    Style and script blocks are dropped first, or their CSS reads as text.
    """
    text = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace(" ", " ")
    return " ".join(text.split())


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _search(pattern: str, text: str, what: str) -> re.Match:
    match = re.search(pattern, text)
    if match is None:
        raise StatementParseError(f"Grab receipt has no {what}")
    return match


def _kind(text: str) -> str:
    if "Your tip goes a long way" in text or "Tips E-Receipt" in text:
        return "tip"
    if "Hope you enjoyed your food" in text:
        return "food"
    if "Hope you enjoyed your ride" in text:
        return "ride"
    raise StatementParseError(
        "not a Grab receipt layout this parser knows (food, ride or tip); "
        "it has been left unlabelled so it can be read once one is added"
    )


def _when(text: str, kind: str) -> datetime:
    """When the money moved, as a Manila wall-clock time."""
    stamped = re.search(r"(\d{1,2} [A-Z][a-z]{2} \d{2} \d{2}:\d{2}) ([+-]\d{4})", text)
    if stamped:
        moment = datetime.strptime(f"{stamped.group(1)} {stamped.group(2)}", "%d %b %y %H:%M %z")
        return moment.astimezone(_LOCAL).replace(tzinfo=None)
    if kind == "ride":
        day = _search(r"Picked up on (\d{1,2} [A-Z][a-z]+ \d{4})", text, "pick-up date")
        return datetime.strptime(day.group(1), "%d %B %Y")
    raise StatementParseError(f"Grab {kind} receipt has no date")


def _paid_by(text: str) -> list[Payment]:
    """Each method in the ``Paid by`` block, with what it paid.

    The block ends at the first thing that is plainly not a payment line;
    an older ride layout prints only the last four card digits as the method.
    """
    block = _search(
        r"Paid by" + _GAP + r"(.*?)(?:Got an issue|Learn more|Say goodbye|©|$)",
        text, "'Paid by' section",
    ).group(1)
    payments = [
        Payment(method=" ".join(method.split()), amount=_money(amount))
        for method, amount in re.findall(
            r"([A-Za-z][\w .'-]*?|\d{4})" + _GAP + _PESO + _AMOUNT, block
        )
    ]
    if not payments:
        raise StatementParseError("Grab receipt's 'Paid by' section lists no payment")
    return payments


def _food(text: str) -> tuple[Decimal, list[Payment], str]:
    headline = _money(_search(r"TOTAL" + _GAP + _PESO + _AMOUNT, text, "total").group(1))
    total = _money(
        _search(r"TOTAL \(INCL\. TAX\)" + _GAP + _PESO + _AMOUNT, text, "TOTAL (INCL. TAX)").group(1)
    )
    if headline != total:
        raise StatementParseError(
            f"Grab food receipt prints two totals that disagree: {headline} and {total}"
        )
    method = _search(
        r"Payment Method:" + _GAP + r"(.+?)" + _GAP + r"(?:Description:|Amount:|Subtotal)",
        text, "payment method",
    ).group(1)
    shop = _search(
        r"Pick up location:" + _GAP + r"(.+?)" + _GAP + r"(?:Drop off location:|Profile:|Receipt Summary)",
        text, "restaurant",
    ).group(1)
    # "Sample Grill - Example Mall" -> "Sample Grill". The branch would make
    # every outlet its own payee, and the brand is what categorises it.
    payee = shop.split(" - ")[0].strip() or shop
    return total, [Payment(method=" ".join(method.split()), amount=total)], payee


def _total_paid(text: str) -> Decimal:
    return _money(_search(r"Total(?: Paid)?" + _GAP + _PESO + _AMOUNT, text, "total").group(1))


def parse_receipt(path, password: str = "") -> Statement:
    """Read one Grab e-receipt email body (HTML or text) into a statement."""
    raw = Path(path).read_bytes().decode("utf-8", errors="replace")
    text = _flatten(raw)

    kind = _kind(text)
    booking = _search(_BOOKING.pattern, text, "booking ID").group(1)
    posted_at = _when(text, kind)

    if kind == "food":
        total, payments, payee = _food(text)
        reference = booking
    else:
        total = _total_paid(text)
        payments = _paid_by(text)
        paid = sum((p.amount for p in payments), Decimal("0"))
        if paid != total:
            raise StatementParseError(
                f"Grab {kind} receipt totals {total} but its payments add up to {paid}"
            )
        payee = "Grab Driver" if kind == "tip" else "GRAB"
        reference = f"{booking}-TIP" if kind == "tip" else booking

    methods = ", ".join(p.method for p in payments)
    period = posted_at.date()

    if len(payments) > 1 and any(p.method == WALLET for p in payments):
        # Part wallet, part something else. Importing the wallet share alone
        # is a guess about how Grab splits it, so ask instead.
        raise StatementParseError(
            f"Grab {kind} receipt {booking} was split across {methods}; "
            "import it by hand"
        )

    if [p.method for p in payments] != [WALLET]:
        return Statement(
            period_start=period, period_end=period,
            starting_balance=None, ending_balance=None,
            total_debit=None, total_credit=None,
            transactions=[],
            notes=(
                f"{kind} {booking} for {total:,.2f} was paid by {methods}, not "
                f"{WALLET}; not imported",
            ),
        )

    return Statement(
        period_start=period, period_end=period,
        starting_balance=None, ending_balance=None,
        total_debit=total, total_credit=None,
        transactions=[
            Transaction(
                posted_at=posted_at,
                description=payee,
                reference_no=reference,
                debit=total,
            )
        ],
    )


BANK = BankDefinition(
    name="grab",
    display_name="GrabPay",
    parser=parse_receipt,
    gmail_query='from:no-reply@grab.com subject:"Your Grab E-Receipt"',
    dkim_domains=("grab.com",),
    content="email_body",
)
