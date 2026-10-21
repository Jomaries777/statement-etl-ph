"""Tests for the statement models, including recovering rows the statement never printed."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def _row(day, hour, desc, ref, debit=None, credit=None, balance=None):
    from statement_etl_ph.models import Transaction
    return Transaction(
        posted_at=datetime(2026, 9, day, hour, 0),
        description=desc,
        reference_no=ref,
        debit=Decimal(debit) if debit else None,
        credit=Decimal(credit) if credit else None,
        balance=Decimal(balance) if balance is not None else None,
    )


def _statement(rows, *, start, end):
    from statement_etl_ph.models import Statement
    return Statement(
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 7),
        starting_balance=Decimal(start), ending_balance=Decimal(end),
        total_debit=None, total_credit=None, transactions=rows,
    )


def test_a_refund_with_no_printed_row_is_recovered():
    """The case this exists for: Angkas takes 59.00, refunds it, prints no
    credit row.

    The balance column jumps back up and the transaction table never mentions
    it, so the statement's own arithmetic stops closing. Modelled on a real
    statement, with every balance and reference number changed.
    """
    st = _statement(
        [
            _row(7, 6, "Payment to Angkas", "0000200000011", debit="59.00", balance="941.00"),
            # 941.00 - 115.00 = 826.00, but it prints 885.00: the 59.00 came back.
            _row(7, 8, "Payment to SAMPLE BAKERY", "0000200000022", debit="115.00", balance="885.00"),
            _row(7, 10, "Received GCash from GoTyme Bank", "8000200000033", credit="5000.00", balance="5885.00"),
        ],
        start="1000.00", end="5885.00",
    )

    recovered = st.recover_unprinted_rows()

    assert len(recovered) == 1
    got = recovered[0]
    assert got.amount == Decimal("59.00")
    assert got.merchant == "Angkas"
    assert got.offsets.reference_no == "0000200000011"
    # Identity derives from the row it reverses, so re-parsing is stable.
    assert got.reference_no == "R0000200000011"
    assert got.as_transaction().credit == Decimal("59.00")


def test_a_gap_that_matches_nothing_is_left_alone():
    """Balance-column drift must not become a transaction.

    reconcile() documents that this column drifts by whole transactions. A gap
    is only evidence of a refund when it is the previous charge negated; an
    arbitrary one is noise, and inventing a row from it would put a number in
    the budget that nothing supports.
    """
    st = _statement(
        [
            _row(7, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00"),
            # 941.00 - 115.00 = 826.00, but it prints 900.00: a 74.00 gap that
            # offsets nothing.
            _row(7, 8, "Payment to SAMPLE BAKERY", "A2", debit="115.00", balance="900.00"),
        ],
        start="1000.00", end="900.00",
    )

    assert st.recover_unprinted_rows() == []


def test_an_unattributable_gap_is_reported_with_where_it_sits():
    """The other half of recovery: a gap that reverses nothing is still real.

    Modelled on a real statement, with every balance changed. The balance drops
    1,515.44 across the GCredit row, which prints a debit of only 1,296.89 —
    so 218.55 left the account and GCash printed no row for it. It offsets no
    charge, so it is never imported; it is reported, and named on the row it
    sits before, which is the only place the statement dates it to.
    """
    st = _statement(
        [
            _row(11, 11, "Payment to Grab Philippines", "0000200000044",
                 debit="356.00", balance="2644.00"),
            _row(13, 6, "GCredit EXMPL1", "8000200000055",
                 debit="1296.89", balance="1128.56"),
        ],
        start="3000.00", end="1128.56",
    )

    assert st.recover_unprinted_rows() == [], "nothing here reverses a charge"

    gaps = st.unexplained_gaps()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.amount == Decimal("-218.55"), "signed: money left the account"
    assert gap.at.reference_no == "8000200000055", "flagged on the row it precedes"
    assert gap.after.reference_no == "0000200000044"
    assert gap.describes == (
        "statement prints no row for 218.55 that left the account "
        "between Sep 11 and Sep 13"
    )


def test_a_recovered_refund_is_never_also_reported_as_unexplained():
    """One walk decides which bucket a gap lands in, so it cannot be both.

    Counting a recovered refund as unexplained too would double-report it and
    make the reported net wrong by twice its amount.
    """
    st = _statement(
        [
            _row(7, 6, "Payment to Angkas", "0000200000011", debit="59.00", balance="941.00"),
            _row(7, 8, "Payment to SAMPLE BAKERY", "0000200000022", debit="115.00", balance="885.00"),
        ],
        start="1000.00", end="885.00",
    )

    assert len(st.recover_unprinted_rows()) == 1
    assert st.unexplained_gaps() == []


def test_a_zero_balance_row_does_not_break_the_walk():
    """GCash prints 0.00 for some merchant payments — four rows a statement.

    Treating that as a real balance does not merely lose a check: it can
    manufacture a refund. Anchoring on a false zero makes the next row's
    expected balance wrong by exactly the zeroed row's amount, and that gap
    then looks like a perfect offset of it.

    Here 1000.00 - 10.00 - 5.00 should reach 985.00. Believing the printed
    0.00 would instead expect -5.00, see 5.00, and "recover" a 10.00 refund
    of a payment that was never refunded.
    """
    st = _statement(
        [
            _row(7, 6, "Payment to Some Merchant", "A1", debit="10.00", balance="0"),
            _row(7, 8, "Payment to Another", "A2", debit="5.00", balance="5.00"),
        ],
        start="1000.00", end="5.00",
    )

    assert st.recover_unprinted_rows() == [], "a printed 0.00 must not anchor the walk"


def test_one_bad_balance_does_not_cascade_into_every_later_row():
    """After a mismatch the walk re-anchors on what the bank printed.

    Carrying our own expectation forward instead leaves every later row short
    by the same gap — and any later charge of that same amount then looks like
    it was refunded too. Angkas charges 59.00 repeatedly, so this is not a
    hypothetical: one real refund would manufacture a second, and the account
    would end up 59.00 too high.
    """
    st = _statement(
        [
            _row(7, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00"),
            # The real refund: expected 826.00, printed 885.00.
            _row(7, 8, "Payment to SAMPLE BAKERY", "A2", debit="115.00", balance="885.00"),
            _row(7, 9, "Payment to Angkas", "A3", debit="59.00", balance="826.00"),
            # Correct from the re-anchored balance. Without re-anchoring this
            # shows a 59.00 gap against the 59.00 row before it.
            _row(7, 10, "Payment to SAMPLE BAKERY", "A4", debit="115.00", balance="711.00"),
        ],
        start="1000.00", end="711.00",
    )

    recovered = st.recover_unprinted_rows()

    assert [r.amount for r in recovered] == [Decimal("59.00")], (
        "exactly one refund: the second would be an artefact of not re-anchoring"
    )


def test_nothing_is_recovered_without_a_starting_balance():
    st = _statement(
        [_row(7, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00")],
        start="0", end="941.00",
    )
    object.__setattr__(st, "starting_balance", None)
    assert st.recover_unprinted_rows() == []


def test_a_refund_that_lands_after_other_merchants_is_still_matched():
    """GCash posts the refund just before the next booking, not after the charge.

    A sequence seen in practice: Angkas takes 59.00, a 749.00 GOMO bill
    posts, then the refund arrives, then the rider is booked again. The row
    before the gap is GOMO, so a rule looking only at the immediately
    preceding row misses it — as it did, twice, leaving 118.00 unaccounted.
    """
    st = _statement(
        [
            _row(2, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00"),
            _row(2, 6, "Payment to GOMO", "A2", debit="749.00", balance="192.00"),
            # 192.00 - 59.00 = 133.00, but it prints 192.00: the 59.00 came back.
            _row(2, 6, "Payment to Angkas", "A3", debit="59.00", balance="192.00"),
        ],
        start="1000.00", end="192.00",
    )

    recovered = st.recover_unprinted_rows()

    assert len(recovered) == 1
    assert recovered[0].amount == Decimal("59.00")
    assert recovered[0].offsets.reference_no == "A1", "the earlier Angkas charge"


def test_a_charge_is_only_reversed_once():
    """Two gaps of 59.00, one Angkas charge — only the first may claim it.

    Angkas charges 59.00 over and over, so without marking a charge spent the
    second gap re-matches the first charge and invents a refund the balance
    column never showed. The second gap here has no charge left to reverse and
    must be left alone, which is the safety rule working rather than failing.
    """
    st = _statement(
        [
            _row(2, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00"),
            # real refund of A1: expected 826.00, printed 885.00
            _row(2, 7, "Payment to SAMPLE BAKERY", "A2", debit="115.00", balance="885.00"),
            # another 59.00 gap: expected 810.00, printed 869.00 — but A1 is spent
            _row(2, 8, "Payment to Kitchen City", "A3", debit="75.00", balance="869.00"),
        ],
        start="1000.00", end="869.00",
    )

    recovered = st.recover_unprinted_rows()

    assert [r.offsets.reference_no for r in recovered] == ["A1"], (
        "the second gap must not re-claim a charge already reversed"
    )


def test_the_nearest_matching_charge_wins():
    """Two identical charges, one refund — it pairs with the closer one.

    Both are equally valid arithmetically; taking the nearest keeps the refund
    beside the booking it belongs to rather than dating it days earlier.
    """
    st = _statement(
        [
            _row(1, 6, "Payment to Angkas", "OLD", debit="59.00", balance="941.00"),
            _row(3, 6, "Payment to Angkas", "NEW", debit="59.00", balance="882.00"),
            # 882.00 - 115.00 = 767.00, prints 826.00
            _row(3, 7, "Payment to SAMPLE BAKERY", "A3", debit="115.00", balance="826.00"),
        ],
        start="1000.00", end="826.00",
    )

    recovered = st.recover_unprinted_rows()

    assert len(recovered) == 1
    assert recovered[0].offsets.reference_no == "NEW"


def test_first_day_falls_back_to_the_rows_when_no_period_was_printed():
    """A statement can parse with no printed period at all; anything keyed
    on the period alone would then have nothing to go on."""
    st = _statement(
        [
            _row(6, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00"),
            _row(3, 9, "Payment to Globe", "G1", debit="199.00", balance="742.00"),
        ],
        start="1000.00", end="742.00",
    )
    object.__setattr__(st, "period_start", None)
    object.__setattr__(st, "period_end", None)

    assert st.first_day == date(2026, 9, 3)


def test_first_day_prefers_the_printed_period():
    st = _statement(
        [_row(6, 6, "Payment to Angkas", "A1", debit="59.00", balance="941.00")],
        start="1000.00", end="941.00",
    )
    assert st.first_day == date(2026, 9, 1)
