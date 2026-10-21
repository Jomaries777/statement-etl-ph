"""Tests for the Grab e-receipt parser (banks/grab.py) and the email-body source.

The receipts below follow the real layouts: same wording and table order,
with the marketing blocks removed. Every name, place, booking code and card
number in them is invented. The peso sign is written as an entity in some,
as Gmail delivers it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from statement_etl_ph.models import StatementParseError
from statement_etl_ph.banks.grab import parse_receipt
from statement_etl_ph.banks import get_bank

_STYLE = "<head><style>td { font-family: Arial; } .x { color: #00B14F; }</style></head>"


def _rows(*cells: str) -> str:
    return "".join(f"<tr><td>{c}</td></tr>" for c in cells)


def food(method="GrabPay Wallet", total="538.00", headline=None) -> str:
    headline = headline or total
    return (
        f"<html>{_STYLE}<body><table>"
        + _rows(
            "Hope you enjoyed your food!",
            f"TOTAL</td><td>&#8369; {headline}",
            "DATE | TIME</td><td>Pick-up time: 11 Oct 26 11:48 +0800",
            "Order Details",
            "Vehicle type: GrabFood",
            "Issued to Juan Dela Cruz",
            "Booking code A-EXAMPLEFOOD01",
            "Pick up location: Sample Grill - Example Mall",
            "Drop off location: Home",
            "Profile: Personal",
            "Receipt Summary",
            f"Payment Method: {method}",
            "Description:</td><td>Amount:",
            "1x Sample Rice Meal</td><td>&#8369; 460.00",
            "Subtotal</td><td>&#8369; 460.00",
            "Delivery Fee</td><td>&#8369; 77.00",
            "Green Programme Fee</td><td>&#8369; 1.00",
            f"TOTAL (INCL. TAX)</td><td>&#8369; {total}",
            "Rate your meal!",
        )
        + "</table></body></html>"
    )


def ride(paid_by=(("GrabPay Wallet", "392.00"),), total="&#8369;392.00") -> str:
    payments = "".join(
        f"<tr><td>{m}</td><td>{a}</td></tr>" for m, a in paid_by
    )
    return (
        f"<html>{_STYLE}<body><table>"
        + _rows(
            "Grab E-Receipt",
            "Standard | Taxi",
            "Hope you enjoyed your ride! Picked up on 06 October 2026 "
            "Booking ID: A-EXAMPLERIDE01",
            f"Total Paid</td><td>{total}",
            "4.8 Compliments for driver Excellent Service",
            "Passenger Juan Dela Cruz Profile PERSONAL</td><td>Paid by",
        )
        + payments
        + _rows(
            "Learn more No more collecting receipts or processing claims.",
            "Got an issue? We've got your back.",
            "Your Trip 8.75 km • 35 mins",
            "1 Example Street 7:40PM",
            "2 Sample Avenue 8:15PM",
            "© Grab 2026",
        )
        + "</table></body></html>"
    )


def tip(method="GrabPay Wallet") -> str:
    return (
        f"<html>{_STYLE}<body><table>"
        + _rows(
            "Grab Tips E-Receipt",
            "GrabFood",
            "Thanks! Your tip goes a long way for your driver. 08 Oct 26 11:25 +0800",
            "Total</td><td>P 50.00",
            "Tip From: 3 Example Road, Sample City Date: 08 Oct 26 11:25 +0800 "
            "Booking ID: A-EXAMPLETIP001</td><td>50.00",
            "Total</td><td>50.00 100% goes to your driver.",
            "Issued by driver Pedro Santos Reyes Issued to Juan Dela "
            "Cruz</td><td>Paid by",
            f"{method}</td><td></td><td>50.00",
            "© Grab 2026",
        )
        + "</table></body></html>"
    )


def _parse(tmp_path, body: str, name="receipt.html"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return parse_receipt(path, "")


# --- What gets imported -------------------------------------------------------


def test_a_wallet_food_order_is_one_row_named_after_the_restaurant(tmp_path):
    statement = _parse(tmp_path, food())
    assert statement.reconcile().ok
    [row] = statement.transactions
    assert row.debit == Decimal("538.00")
    assert row.signed_amount == Decimal("-538.00")
    assert row.posted_at == datetime(2026, 10, 11, 11, 48)
    assert row.reference_no == "A-EXAMPLEFOOD01"
    # The branch would make every outlet its own payee; the brand categorises.
    assert row.merchant == "Sample Grill"


def test_a_wallet_ride_is_one_row_paid_to_grab(tmp_path):
    statement = _parse(tmp_path, ride())
    [row] = statement.transactions
    assert row.debit == Decimal("392.00")
    assert row.value_date == date(2026, 10, 6)
    assert row.reference_no == "A-EXAMPLERIDE01"
    assert row.merchant == "GRAB"


def test_a_tip_does_not_collide_with_the_order_it_tips(tmp_path):
    """The tip carries its order's booking ID. Without a distinct reference
    YNAB would reject it as a duplicate of the order and it would vanish."""
    order = _parse(tmp_path, food(), "order.html").transactions[0]
    tipped = _parse(tmp_path, tip(), "tip.html").transactions[0]
    assert tipped.debit == Decimal("50.00")
    assert tipped.merchant == "Grab Driver"
    assert tipped.reference_no == "A-EXAMPLETIP001-TIP"
    assert tipped.reference_no != order.reference_no


def test_the_duplicate_key_is_the_booking_code(tmp_path):
    statement = _parse(tmp_path, tip())
    assert statement.transactions[0].duplicate_key == "A-EXAMPLETIP001-TIP"


def test_plain_text_bodies_parse_the_same_way(tmp_path):
    """Gmail can deliver the receipt as text with table cells as pipes."""
    body = (
        "| Hope you enjoyed your food! |\n| TOTAL ₱ 538.00 | DATE | TIME "
        "Pick-up time: 11 Oct 26 11:48 +0800 |\n| Booking code A-EXAMPLEFOOD01 |\n"
        "| Pick up location: Sample Grill - Example Mall |\n\n| |\n"
        "| Drop off location: Home |\n| | Payment Method: GrabPay Wallet | |\n"
        "| | | |\n| | Description: | Amount: | |\n"
        "| | TOTAL (INCL. TAX) | ₱ 538.00 | |\n"
    )
    [row] = _parse(tmp_path, body, "receipt.txt").transactions
    assert row.debit == Decimal("538.00")
    assert row.merchant == "Sample Grill"


# --- What does not ------------------------------------------------------------


@pytest.mark.parametrize(
    "body, method",
    [
        (food(method="Visa 4242", total="482.00"), "Visa 4242"),
        (ride(paid_by=(("GCash", "392.00"),)), "GCash"),
        # The older ride layout prints only the card's last four digits.
        (ride(paid_by=(("4242", "616.00"),), total="P 616.00"), "4242"),
        (tip(method="GCash"), "GCash"),
    ],
)
def test_anything_not_paid_from_the_wallet_imports_nothing(tmp_path, body, method):
    """Card and GCash payments are already on those accounts' own statements;
    importing them into GrabPay as well would count them twice."""
    statement = _parse(tmp_path, body)
    assert statement.transactions == []
    assert statement.reconcile().ok
    [note] = statement.notes
    assert method in note
    assert "not imported" in note


def test_a_split_payment_is_refused_rather_than_guessed(tmp_path):
    body = ride(paid_by=(("GrabPay Wallet", "200.00"), ("Visa 4242", "192.00")))
    with pytest.raises(StatementParseError, match="split"):
        _parse(tmp_path, body)


def test_payments_that_do_not_add_up_to_the_total_are_a_misread(tmp_path):
    body = ride(paid_by=(("GrabPay Wallet", "390.00"),))
    with pytest.raises(StatementParseError, match="add up"):
        _parse(tmp_path, body)


def test_a_food_receipt_whose_two_totals_disagree_is_a_misread(tmp_path):
    with pytest.raises(StatementParseError, match="disagree"):
        _parse(tmp_path, food(headline="583.00"))


def test_an_unknown_layout_fails_loudly(tmp_path):
    """GrabMart and Express share the subject. A layout this code has not seen
    must show up in the run report, not be read as something it is not."""
    body = "<html><body>Your GrabMart order is complete. TOTAL ₱ 120.00</body></html>"
    with pytest.raises(StatementParseError, match="layout"):
        _parse(tmp_path, body)


def test_the_parser_is_registered():
    bank = get_bank("grab")
    assert bank.parser is parse_receipt
    assert bank.reads_body


# --- Fetching the body ------------------------------------------------------------


def _part(mime, text="", filename=""):
    import base64

    return {
        "mimeType": mime,
        "filename": filename,
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
    }


def test_the_html_body_is_preferred_over_the_plain_one():
    """Taking both would print every amount twice."""
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/plain", "plain"), _part("text/html", "<b>html</b>")],
    }
    assert client._find_body_part(payload)["mimeType"] == "text/html"


def test_an_attached_html_file_is_not_mistaken_for_the_body():
    from statement_etl_ph.gmail.fetch import GmailClient

    client = GmailClient.__new__(GmailClient)
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _part("text/html", "<b>attached</b>", filename="invoice.html"),
            _part("text/plain", "the body"),
        ],
    }
    assert client._find_body_part(payload)["mimeType"] == "text/plain"


def test_fetching_a_body_source_returns_the_html(monkeypatch):
    from statement_etl_ph.gmail.fetch import GmailClient

    from test_gmail_fetch import _FakeGmailService, _auth_results

    message = {
        "id": "m1", "threadId": "t1", "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Your Grab E-Receipt"},
                {"name": "Authentication-Results", "value": _auth_results(domain="grab.com")},
            ],
            "mimeType": "multipart/alternative",
            "parts": [_part("text/html", food())],
        },
    }
    client = GmailClient.__new__(GmailClient)
    client._service = _FakeGmailService(message)
    fetched = client.fetch_statement("m1", body_only=True, dkim_domains=("grab.com",))
    assert fetched.filename == "m1.html"
    assert b"GrabPay Wallet" in fetched.content
