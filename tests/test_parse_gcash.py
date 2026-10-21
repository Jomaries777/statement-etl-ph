"""Tests for the GCash PDF parser (banks/gcash.py)."""

from __future__ import annotations

from decimal import Decimal
import pytest

from statement_etl_ph.banks.gcash import parse_statement

from conftest import FIXTURE, GCASH_PASSWORD
from make_fixture import ROWS, build


def test_all_rows_are_extracted(statement):
    assert len(statement.transactions) == 8


def test_period_and_totals_are_read(statement):
    assert statement.period_start.isoformat() == "2026-08-22"
    assert statement.period_end.isoformat() == "2026-08-28"
    assert statement.starting_balance == Decimal("4003.68")
    assert statement.total_debit == Decimal("2505.50")
    assert statement.total_credit == Decimal("1500.00")


def test_statement_reconciles(statement):
    result = statement.reconcile()
    assert result.ok, result.errors


def test_wrong_password_is_rejected():
    with pytest.raises(Exception):
        parse_statement(FIXTURE, "definitely-not-the-password")


def test_credit_row_becomes_a_positive_amount(statement):
    """The Credit column is money in, so it must come out positive."""
    inflow = [t for t in statement.transactions if t.credit]
    assert len(inflow) == 1
    assert inflow[0].credit == Decimal("1500.00")
    assert inflow[0].milliunits == 1_500_000
    assert inflow[0].signed_amount > 0


def test_debit_rows_become_negative_amounts(statement):
    outflows = [t for t in statement.transactions if t.debit]
    assert len(outflows) == 7
    assert all(t.milliunits < 0 for t in outflows)


def test_thousands_separator_is_parsed(statement):
    power = next(t for t in statement.transactions if "Example Power" in t.merchant)
    assert power.debit == Decimal("1250.00")
    assert power.milliunits == -1_250_000


def test_fractional_amount_survives_milliunit_conversion(statement):
    grocer = next(t for t in statement.transactions if "Sample Grocer" in t.merchant)
    assert grocer.debit == Decimal("31.50")
    assert grocer.milliunits == -31_500


def test_every_reference_and_description_is_read_exactly(statement):
    """Counting distinct references is not enough: a description glyph that
    merges into the reference column still yields a unique, wrong number."""
    import sys

    sys.path.insert(0, str(FIXTURE.parent))
    from make_fixture import ROWS

    assert [t.reference_no for t in statement.transactions] == [row[3] for row in ROWS]
    assert [t.description for t in statement.transactions] == [row[2] for row in ROWS]


def test_every_row_has_a_reference_number(statement):
    assert all(t.reference_no for t in statement.transactions)
    assert len({t.reference_no for t in statement.transactions}) == 8


def test_merchant_name_is_cleaned(statement):
    """'Payment to X, Merchant Transaction Number: ...' should reduce to 'X'."""
    apps = next(t for t in statement.transactions if "Example Apps" in t.merchant)
    assert apps.merchant == "Example Apps"
    assert "Merchant Transaction Number" not in apps.merchant
    assert not any(t.merchant.startswith("Payment to") for t in statement.transactions)


def test_zero_balance_row_is_kept(statement):
    """GCash prints 0.00 balances; the transaction is still real."""
    kitchen = next(t for t in statement.transactions if "Sample Kitchen" in t.merchant)
    assert kitchen.balance == Decimal("0.00")
    assert kitchen.debit == Decimal("75.00")


def test_reconciliation_catches_a_dropped_row(statement):
    """If extraction loses a row, the totals must not silently agree."""
    from statement_etl_ph.models import Statement

    damaged = Statement(
        period_start=statement.period_start,
        period_end=statement.period_end,
        starting_balance=statement.starting_balance,
        ending_balance=statement.ending_balance,
        total_debit=statement.total_debit,
        total_credit=statement.total_credit,
        transactions=statement.transactions[:-1],
    )
    result = damaged.reconcile()
    assert not result.ok
    assert any("Total Debit" in e for e in result.errors)


def test_unreadable_pdf_raises(tmp_path):
    junk = tmp_path / "not-a-statement.pdf"
    junk.write_bytes(b"%PDF-1.4\nnot really\n")
    with pytest.raises(Exception):
        parse_statement(junk, "x")


def test_every_reference_and_description_is_read_exactly(statement):
    """Counting distinct references is not enough: a description glyph that
    merges into the reference column still yields a unique, wrong number."""
    assert [t.reference_no for t in statement.transactions] == [row[3] for row in ROWS]
    assert [t.description for t in statement.transactions] == [row[2] for row in ROWS]


def _with_blank_inbound_transfer(rows, after: int, amount: str):
    """The statement with an inbound bank transfer GCash prints with no
    description, inserted after row ``after``, every later balance shifted."""
    shift = Decimal(amount)
    blank = (rows[after][0], "11:59 PM", "", "0000100000099", "", amount,
             f"{Decimal(rows[after][6]) + shift:.2f}")
    later = [
        (*row[:6], row[6] if row[6] == "0.00" else f"{Decimal(row[6]) + shift:.2f}")
        for row in rows[after + 1:]
    ]
    return [*rows[: after + 1], blank, *later]


def test_a_row_with_no_description_keeps_every_amount_on_its_own_row(tmp_path):
    """GCash prints inbound bank transfers with no description at all.

    A parser that pairs a list of descriptions with a list of amounts by
    position shifts every later amount onto the wrong row and drops the last
    one, without an error. Reading the page as a grid keeps the blank row, and
    reconcile() proves nothing moved: the rows still add up to the totals.
    """
    rows = _with_blank_inbound_transfer(ROWS, after=2, amount="500.00")
    pdf = build(
        tmp_path / "blank.pdf",
        rows,
        ending_balance="3498.18",
        total_credit="2000.00",
    )
    parsed = parse_statement(pdf, GCASH_PASSWORD)

    assert len(parsed.transactions) == len(ROWS) + 1
    blank = parsed.transactions[3]
    assert blank.description == ""
    assert blank.credit == Decimal("500.00")
    assert blank.reference_no == "0000100000099"
    for txn, row in zip(parsed.transactions, rows):
        assert txn.reference_no == row[3]
        assert txn.debit == (Decimal(row[4].replace(",", "")) if row[4] else None)
        assert txn.credit == (Decimal(row[5].replace(",", "")) if row[5] else None)
    assert parsed.reconcile().ok, parsed.reconcile().errors
