"""Tests for the GoTyme PDF parser (banks/gotyme.py)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import pytest

from statement_etl_ph.models import Transaction
from statement_etl_ph.banks.gotyme import parse_statement as parse_gotyme
from statement_etl_ph.banks import available, get_bank

from conftest import GOTYME_FIXTURE, GOTYME_PASSWORD


def test_gotyme_statement_reconciles(gotyme_statement):
    """The whole point of reconciliation: our rows must equal the bank's totals."""
    result = gotyme_statement.reconcile()
    assert result.ok, result.errors
    assert gotyme_statement.total_credit == Decimal("2399.00")
    assert gotyme_statement.total_debit == Decimal("2082.56")


def test_gotyme_rows_continue_onto_the_second_page(gotyme_statement):
    """The column headers repeat per page; reading only page 1 loses the rest."""
    assert len(gotyme_statement.transactions) == 8
    assert gotyme_statement.transactions[-1].value_date == date(2026, 8, 7)


def test_gotyme_period_is_read(gotyme_statement):
    assert gotyme_statement.period_start == date(2026, 8, 1)
    assert gotyme_statement.period_end == date(2026, 8, 7)


def test_a_zero_in_the_unused_column_is_not_a_credit(gotyme_statement):
    """GoTyme prints 0.00 in the column a row does not use.

    Read literally, every debit row would also carry a credit of zero — and
    `signed_amount` returns the credit first, so every transaction on the
    statement would import as 0.00.
    """
    coffee = next(t for t in gotyme_statement.transactions if "COFFEE" in t.merchant)
    assert coffee.credit is None
    assert coffee.debit == Decimal("150.00")
    assert coffee.signed_amount == Decimal("-150.00")


def test_a_credit_row_is_positive(gotyme_statement):
    refund = next(t for t in gotyme_statement.transactions if "Refund" in t.merchant)
    assert refund.debit is None
    assert refund.signed_amount == Decimal("99.00")


def test_two_identical_payments_on_one_day_stay_distinct(gotyme_statement):
    """GoTyme prints no reference numbers, and this statement has two RIDEHAIL
    payments of 99.00 on the same day.

    An amount+date fallback would give both the same duplicate key, and a
    tracker would keep only the first, silently dropping a real transaction.
    The running balance is what separates them.
    """
    rides = [t for t in gotyme_statement.transactions if t.merchant == "RIDEHAIL"]
    assert len(rides) == 2
    assert rides[0].signed_amount == rides[1].signed_amount
    assert rides[0].value_date == rides[1].value_date
    assert rides[0].reference_no != rides[1].reference_no


def test_every_gotyme_row_gets_its_own_reference(gotyme_statement):
    refs = [t.reference_no for t in gotyme_statement.transactions]
    assert len(set(refs)) == len(refs)


def test_the_synthetic_reference_is_stable_across_runs():
    """Idempotency depends on the same statement yielding the same keys."""
    first = parse_gotyme(GOTYME_FIXTURE, GOTYME_PASSWORD)
    second = parse_gotyme(GOTYME_FIXTURE, GOTYME_PASSWORD)
    assert [t.reference_no for t in first.transactions] == [
        t.reference_no for t in second.transactions
    ]


def test_a_gotyme_duplicate_key_is_its_synthetic_reference(gotyme_statement):
    """Short enough for a tracker's import-ID field (YNAB allows 36)."""
    txn = gotyme_statement.transactions[0]
    assert txn.duplicate_key == txn.reference_no
    assert len(f"GT:{txn.duplicate_key}") <= 36


def test_a_wrapped_description_is_read_as_one_row(gotyme_statement):
    """GoTyme prints long descriptions above AND below the data line.

    Treated as separate lines, the transfer loses its payee and the stray
    fragments look like rows of their own.
    """
    transfer = next(
        t for t in gotyme_statement.transactions if t.value_date == date(2026, 8, 6)
    )
    assert "J. D. SANTOS" in transfer.description
    assert "9999" in transfer.description          # the tail printed below
    assert transfer.credit == Decimal("2000.00")


def test_the_card_suffix_is_stripped_from_the_payee():
    """The real statement masks the card with bullets, which vary per card and
    would otherwise split one merchant into several cache entries."""
    txn = Transaction(
        posted_at=datetime(2026, 8, 1),
        description="Card payment at STARBUCKS COFFEE WEB, Card •••• 1234",
        reference_no="x",
        debit=Decimal("500.00"),
    )
    assert txn.merchant == "STARBUCKS COFFEE WEB"


def test_the_account_number_tail_is_stripped_from_a_transfer():
    txn = Transaction(
        posted_at=datetime(2026, 8, 8),
        description=(
            "Transfer from J. D. SANTOS Maya Philippines, Inc "
            "(PayMaya Wallet), Account No. ••••••••0000"
        ),
        reference_no="x",
        credit=Decimal("3000.00"),
    )
    assert txn.merchant == "J. D. SANTOS Maya Philippines, Inc (PayMaya Wallet)"


def test_gcash_payees_are_unaffected_by_the_gotyme_cleaning(statement):
    """The suffix rules are shared, so they must not disturb GCash rows."""
    merchants = {t.merchant for t in statement.transactions}
    assert "Example Apps" in merchants
    assert "Sample Grocer Inc" in merchants


def test_the_bank_registry_resolves_gotyme():
    assert "gotyme" in available()
    assert get_bank("gotyme").parser is parse_gotyme


def test_a_wrong_password_is_rejected_by_the_gotyme_parser():
    with pytest.raises(Exception):
        parse_gotyme(GOTYME_FIXTURE, "00000000")
