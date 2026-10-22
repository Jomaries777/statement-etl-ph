"""What every parser returns: a :class:`Statement` of :class:`Transaction` rows,
and the checks that prove the rows were read correctly."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


class StatementParseError(RuntimeError):
    """Raised when a statement PDF does not look like one from its provider."""


# Trailing noise a bank prints on every card row: ", Card •••• 1234" and
# ", Account No. ••••••••0000". Varies per card, not per merchant, so it must
# come off before the description is used as a cache key.
_CARD_SUFFIX_RE = re.compile(r",?\s*Card\s*[•*\u2022]+\s*\d{3,4}\s*$")
_ACCOUNT_SUFFIX_RE = re.compile(r",?\s*Account No\.?\s*[•*\u2022]*\d*\s*$")


@dataclass(frozen=True)
class Transaction:
    """One row of a parsed statement table.

    Amounts are kept as ``Decimal`` all the way through so that peso values
    never touch binary floating point. A sink converts them to whatever its
    tracker wants, once, at the end.
    """

    posted_at: datetime
    description: str
    reference_no: str
    debit: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    balance: Optional[Decimal] = None

    @property
    def value_date(self) -> date:
        return self.posted_at.date()

    @property
    def signed_amount(self) -> Decimal:
        """Positive for money in, negative for money out."""
        if self.credit is not None:
            return self.credit
        if self.debit is not None:
            return -self.debit
        return Decimal("0")

    @property
    def milliunits(self) -> int:
        """The signed amount in thousandths of the currency unit."""
        return int((self.signed_amount * 1000).to_integral_value())

    @property
    def duplicate_key(self) -> str:
        """A stable key for spotting the same row imported twice.

        The bank's own reference number when it prints one; GoTyme prints
        none, so its parser synthesises one. Failing both, amount and date,
        which is weaker: two identical payments on one day share it, and
        :meth:`Statement.reconcile` warns when that fallback is in use.
        """
        if self.reference_no:
            return self.reference_no
        return f"{self.milliunits}:{self.value_date.isoformat()}"

    @property
    def merchant(self) -> str:
        """Best-effort merchant name, stripped of GCash boilerplate.

        ``Payment to Example Apps, Merchant Transaction Number: EXMPL00000A1``
        becomes ``Example Apps``, which is what a tracker wants as the payee
        and what a categoriser wants as its key.
        """
        text = self.description.strip()
        # Strip the per-transaction tail before the prefix, so a card row
        # reduces to the merchant and nothing else.
        text = _ACCOUNT_SUFFIX_RE.sub("", text)
        text = _CARD_SUFFIX_RE.sub("", text)
        for prefix in (
            "Payment to ",
            "Card payment at ",
            "Transfer to ",
            "Transfer from ",
            "Sent to ",
            "Received from ",
        ):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        # Drop trailing reference noise that varies per transaction.
        for marker in (", Merchant Transaction Number:", " Merchant Transaction Number:"):
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx]
                break
        return " ".join(text.split()).strip(" ,-")


@dataclass(frozen=True)
class Statement:
    """A parsed statement: the rows plus the figures they are reconciled against."""

    period_start: Optional[date]
    period_end: Optional[date]
    starting_balance: Optional[Decimal]
    ending_balance: Optional[Decimal]
    total_debit: Optional[Decimal]
    total_credit: Optional[Decimal]
    transactions: list[Transaction] = field(default_factory=list)
    # What the parser decided not to turn into rows, said in the run report.
    # A Grab receipt paid by card parses to no rows on purpose, and a run
    # reading "parsed 0 transaction(s)" with no reason looks like a bug.
    notes: tuple[str, ...] = ()

    @property
    def first_day(self) -> Optional[date]:
        """The earliest day this statement speaks for.

        The printed period when there is one, otherwise the earliest row. The
        period header is not reliable: some statements parse with neither
        date, and anything keyed on the period alone would then have nothing
        to go on. The rows themselves always carry dates, and a recovered
        refund is dated by the charge it reverses, so it never predates them.
        """
        earliest = min((t.value_date for t in self.transactions), default=None)
        candidates = [d for d in (self.period_start, earliest) if d is not None]
        if candidates:
            return min(candidates)
        return self.period_end

    def recover_unprinted_rows(self) -> list["RecoveredRow"]:
        """Movements the running balance proves, but the table never printed.

        Walks the balance column: each row's balance should be the previous
        one plus that row's own amount. Where it is not, money moved without a
        row, and the shortfall is exactly what moved.

        Two things stop this from acting on noise.

        A gap is only believed when it **exactly reverses a charge that is
        still unrefunded**, searching backwards from where the gap appears.
        ``reconcile`` documents that this column drifts by whole transactions,
        so an unexplained gap proves nothing on its own.

        The search is backwards rather than strictly at the preceding row
        because a refund does not always land there. GCash posts one just
        before the next booking, so the sequence reads charge, *other
        merchants*, refund, charge — and the row before the gap is somebody
        else entirely. A refund arriving a day after its charge, or landing
        between an unrelated bill and the rebooking it preceded, is missed by
        a rule that looks only at the row before.

        Each charge can be reversed once. Without that, a single gap amount
        would re-match the same charge every time it recurred, and a
        ride-hailing fare is charged at the same amount over and over.

        What is at stake in the search is narrower than it looks: the balance
        column already proves the *amount* and the *date*, because both come
        from the bank's own arithmetic. Attribution decides only the payee and
        the category. A looser rule therefore risks a mislabelled row, not a
        wrong balance — a much smaller failure than inventing a movement.

        Rows printing ``0.00`` — which GCash does for some merchant payments —
        cannot anchor the walk. Rather than skip them and lose the rest of the
        statement, the expected balance is carried across them arithmetically,
        so the next usable row still gets checked.
        """
        return self._walk_balance_column()[0]

    def unexplained_gaps(self) -> list["UnexplainedGap"]:
        """Movements the balance column proves that nothing can attribute.

        The other half of :meth:`recover_unprinted_rows`. The bank's own
        arithmetic insists money moved, but the gap reverses no charge, so
        there is no honest way to name a payee for it — and this codebase
        does not invent one: money leaving between a ride and a credit-line
        repayment, say, with no row printed for it.

        These are reported, never imported. The point is that the difference
        between a tracker and the app has a stated cause sitting next to the
        row it precedes, rather than being a mystery found weeks later.
        """
        return self._walk_balance_column()[1]

    def _walk_balance_column(
        self,
    ) -> tuple[list["RecoveredRow"], list["UnexplainedGap"]]:
        """One ordered pass, splitting gaps by whether they can be attributed.

        Both halves come out of the same walk deliberately: a gap counted as
        a recovered refund must not also be reported as unexplained, and
        which one it is depends on ``reversed_already`` state that only a
        single ordered pass can track.
        """
        recovered: list[RecoveredRow] = []
        unexplained: list[UnexplainedGap] = []
        if self.starting_balance is None:
            return recovered, unexplained

        expected = self.starting_balance
        reversed_already: set[int] = set()

        for index, txn in enumerate(self.transactions):
            expected = expected + txn.signed_amount
            printed = txn.balance

            # 0.00 is GCash declining to print a balance, not a real zero.
            if printed is not None and printed != 0:
                gap = printed - expected
                if gap != 0:
                    charge = self._charge_reversed_by(gap, index, reversed_already)
                    if charge is not None:
                        position, row = charge
                        reversed_already.add(position)
                        recovered.append(
                            RecoveredRow(
                                posted_at=row.posted_at,
                                amount=gap,
                                offsets=row,
                                printed_balance=printed,
                                expected_balance=expected,
                            )
                        )
                    else:
                        unexplained.append(
                            UnexplainedGap(
                                amount=gap,
                                at=txn,
                                after=self.transactions[index - 1] if index else None,
                                printed_balance=printed,
                                expected_balance=expected,
                            )
                        )
                # Re-anchor on what the bank printed, so one bad row does not
                # cascade into every row after it.
                expected = printed

        return recovered, unexplained

    def _charge_reversed_by(
        self, gap: Decimal, before: int, taken: set[int]
    ) -> Optional[tuple[int, Transaction]]:
        """The nearest earlier row this gap exactly reverses, if any.

        Nearest first, so a refund still pairs with the charge it follows when
        the statement prints them adjacently — which is the common case. Rows
        already reversed are skipped so one charge cannot absorb two refunds.
        """
        for position in range(before - 1, -1, -1):
            if position in taken:
                continue
            row = self.transactions[position]
            if gap == -row.signed_amount:
                return position, row
        return None

    def reconcile(self) -> "Reconciliation":
        """Check the parsed rows against the figures the bank printed.

        Two very different kinds of check live here, and conflating them would
        make every statement either a failure or unchecked.

        **Errors** mean *we* parsed the PDF wrong. If the debit rows we found
        do not add up to the ``Total Debit`` GCash printed, we have dropped or
        misread a row, and pushing that into a budget would be worse than
        failing.

        **Warnings** mean *GCash's own* bookkeeping is inconsistent. The
        running Balance column is not trustworthy: real statements print
        ``0.00`` for some rows and drift by whole transactions. That is not
        our bug to fix, and it must not block the import, but the user should
        hear about it because their tracker's balance will not match the app.
        """
        errors: list[str] = []
        warnings: list[str] = []

        summed_debit = sum((t.debit for t in self.transactions if t.debit), Decimal("0"))
        summed_credit = sum((t.credit for t in self.transactions if t.credit), Decimal("0"))

        # --- Errors: our extraction must match the printed totals exactly. ---
        if self.total_debit is not None and summed_debit != self.total_debit:
            errors.append(
                f"debit rows sum to {summed_debit} but the statement's Total Debit "
                f"is {self.total_debit} (difference {summed_debit - self.total_debit:+})"
            )
        if self.total_credit is not None and summed_credit != self.total_credit:
            errors.append(
                f"credit rows sum to {summed_credit} but the statement's Total Credit "
                f"is {self.total_credit} (difference {summed_credit - self.total_credit:+})"
            )

        seen: set[str] = set()
        for t in self.transactions:
            if t.reference_no and t.reference_no in seen:
                errors.append(f"duplicate reference number within one statement: {t.reference_no}")
            seen.add(t.reference_no)

        missing_ref = [t for t in self.transactions if not t.reference_no]
        if missing_ref:
            warnings.append(
                f"{len(missing_ref)} row(s) have no reference number; "
                "these fall back to an amount+date import key, which is a weaker "
                "duplicate guard"
            )

        # --- Warnings: GCash's own balance column. ---
        blank_balances = [t for t in self.transactions if t.balance == 0]
        if blank_balances:
            warnings.append(
                f"{len(blank_balances)} row(s) print a running balance of 0.00, "
                "which GCash does for some merchant payments; the amounts themselves "
                "are unaffected"
            )

        if (
            self.starting_balance is not None
            and self.ending_balance is not None
        ):
            expected = self.starting_balance - summed_debit + summed_credit
            if expected != self.ending_balance:
                warnings.append(
                    f"GCash's arithmetic does not close: starting {self.starting_balance} "
                    f"- debits {summed_debit} + credits {summed_credit} = {expected}, "
                    f"but the printed ending balance is {self.ending_balance} "
                    f"(off by {self.ending_balance - expected:+}). The transaction rows "
                    "still match the printed totals; the notes below say what the "
                    "running balance shows moved without a row."
                )

        return Reconciliation(errors=errors, warnings=warnings)


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of :meth:`Statement.reconcile`."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class RecoveredRow:
    """A movement the balance column proves happened, but no row printed.

    GCash refunds a cancelled booking without printing a credit row: the
    running balance jumps back up, the transaction table does not mention it,
    and the printed Total Credit does not include it either. The difference
    shows up only as a statement whose own arithmetic does not close.

    The balance column is the bank's own record, so reading it is reading the
    statement more completely — not inventing data. But it cannot be trusted
    naively: ``Statement.reconcile`` documents that it prints ``0.00`` on some
    rows and drifts by whole transactions. A gap alone therefore proves
    nothing.

    What makes a gap trustworthy is that it **exactly offsets the row before
    it**. Drift is arbitrary; a refund is the charge back to the centavo. That
    is the whole safety argument for creating a transaction from this, and it
    is why ``offsets`` is not optional — a gap we cannot attribute is reported
    and never invented.
    """

    posted_at: datetime
    amount: Decimal
    offsets: Transaction
    printed_balance: Decimal
    expected_balance: Decimal

    @property
    def merchant(self) -> str:
        return self.offsets.merchant

    @property
    def reference_no(self) -> str:
        # Derived from the row it reverses, so the same refund recovered from
        # a re-parsed statement keeps the same identity.
        return f"R{self.offsets.reference_no}"

    def as_transaction(self) -> Transaction:
        """The recovered movement as an ordinary row.

        Returning a Transaction rather than a special case means everything
        downstream — deduplication, categorisation, the sink — treats it
        like any other row and needs no knowledge of where it came from.
        """
        credit = self.amount if self.amount > 0 else None
        debit = -self.amount if self.amount < 0 else None
        return Transaction(
            posted_at=self.posted_at,
            description=self.offsets.description,
            reference_no=self.reference_no,
            debit=debit,
            credit=credit,
            balance=self.printed_balance,
        )


@dataclass(frozen=True)
class UnexplainedGap:
    """Money the balance column proves moved, with nothing to attribute it to.

    ``amount`` carries the direction the money went: negative means it left
    the account with no row printed for it. The statement dates it no more
    precisely than "between these two rows", so that is what this records —
    ``after`` is the last row whose balance was right, ``at`` is the row
    whose printed balance exposed the gap.

    Deliberately not a :class:`RecoveredRow`: that class exists for movements
    this code is willing to import, and its whole safety argument is
    that a recovered row exactly reverses a known charge. This one reverses
    nothing, so it is reported and never imported.
    """

    amount: Decimal
    at: Transaction
    after: Optional[Transaction]
    printed_balance: Decimal
    expected_balance: Decimal

    @property
    def describes(self) -> str:
        """One line naming the gap and where it sits.

        Short enough for a transaction memo, which is where this usually ends up:
        the point is that someone looking at the row right after the gap can
        see why their balance stopped matching the app.
        """
        # Built by hand rather than with %-d, which is a glibc extension and
        # not portable off Linux.
        def day(txn: Transaction) -> str:
            return f"{txn.posted_at:%b} {txn.posted_at.day}"

        if self.after is not None:
            window = f"{day(self.after)} and {day(self.at)}"
        else:
            window = f"the opening balance and {day(self.at)}"
        direction = "left" if self.amount < 0 else "entered"
        return (
            f"statement prints no row for {abs(self.amount):,.2f} that "
            f"{direction} the account between {window}"
        )

