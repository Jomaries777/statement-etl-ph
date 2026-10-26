"""Tests for the CSV and Money Manager sinks."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal

from statement_etl_ph.models import Transaction
from statement_etl_ph.sinks import SINKS, available
from statement_etl_ph.sinks.export import CsvSink
from statement_etl_ph.sinks.money_manager import (
    COLUMNS,
    MoneyManagerConverter,
    MoneyManagerSink,
)


def _txn(desc="Payment to Example Apps", ref="0000100000066", debit=None, credit=None):
    return Transaction(
        posted_at=datetime(2026, 8, 26, 9, 9),
        description=desc,
        reference_no=ref,
        debit=Decimal(debit) if debit else None,
        credit=Decimal(credit) if credit else None,
    )


def _read(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- Money Manager ----------------------------------------------------------


def test_the_columns_are_money_managers_in_its_order(tmp_path, statement):
    path = tmp_path / "mm.csv"
    MoneyManagerSink(path).send("GCash", statement.transactions)
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == [
        "Date", "Account", "Category", "Subcategory", "Note",
        "Amount", "Income/Expense", "Description",
    ]
    assert tuple(header) == COLUMNS


def test_a_credit_is_income_and_a_debit_is_expense():
    convert = MoneyManagerConverter()
    assert convert.row(_txn(credit="1500.00"), "GCash")["Income/Expense"] == "Income"
    assert convert.row(_txn(debit="489.00"), "GCash")["Income/Expense"] == "Expense"


def test_amounts_are_positive_plain_decimals():
    convert = MoneyManagerConverter()
    assert convert.row(_txn(debit="1250"), "GCash")["Amount"] == "1250.00"
    assert convert.row(_txn(credit="31.5"), "GCash")["Amount"] == "31.50"


def test_category_subcategory_and_note_are_blank():
    row = MoneyManagerConverter().row(_txn(debit="10.00"), "GCash")
    assert row["Category"] == row["Subcategory"] == row["Note"] == ""


def test_the_date_is_the_statements_and_its_format_is_configurable():
    row = MoneyManagerConverter().row(_txn(debit="10.00"), "GCash")
    assert row["Date"] == "2026-08-26"
    row = MoneyManagerConverter("%m/%d/%Y").row(_txn(debit="10.00"), "GCash")
    assert row["Date"] == "08/26/2026"


def test_the_account_is_whatever_the_caller_names():
    assert MoneyManagerConverter().row(_txn(debit="1.00"), "My GCash")["Account"] == "My GCash"


def test_the_description_is_the_merchant_and_its_reference():
    row = MoneyManagerConverter().row(
        _txn("Payment to Example Apps, Merchant Transaction Number: EXMPL00000A1",
             debit="489.00"),
        "GCash",
    )
    assert row["Description"] == "Example Apps (ref 0000100000066)"


def test_a_row_without_a_reference_is_just_the_merchant():
    row = MoneyManagerConverter().row(_txn(ref="", debit="5.00"), "GCash")
    assert row["Description"] == "Example Apps"


def test_a_row_with_no_description_still_gets_a_name():
    """GCash prints inbound bank transfers with no description."""
    row = MoneyManagerConverter().row(_txn(desc="", credit="500.00"), "GCash")
    assert row["Description"] == "GCash transaction (ref 0000100000066)"


def test_a_row_that_moved_no_money_is_skipped_and_reported(tmp_path):
    result = MoneyManagerSink(tmp_path / "mm.csv").send(
        "GCash", [_txn(debit="5.00"), _txn(ref="0000100000077")]
    )
    assert result.created == ["0000100000066"]
    assert result.skipped == [("0000100000077", "no debit or credit amount")]
    assert len(_read(tmp_path / "mm.csv")) == 1


def test_the_synthetic_statement_becomes_eight_rows_that_add_up(tmp_path, statement):
    path = tmp_path / "mm.csv"
    MoneyManagerSink(path).send("GCash", statement.transactions)
    rows = _read(path)
    assert len(rows) == 8
    income = [r for r in rows if r["Income/Expense"] == "Income"]
    expense = [r for r in rows if r["Income/Expense"] == "Expense"]
    assert len(income) == 1 and len(expense) == 7
    assert sum(Decimal(r["Amount"]) for r in expense) == statement.total_debit
    assert sum(Decimal(r["Amount"]) for r in income) == statement.total_credit


def test_writing_the_same_statement_twice_replaces_the_file(tmp_path, statement):
    path = tmp_path / "mm.csv"
    MoneyManagerSink(path).send("GCash", statement.transactions)
    MoneyManagerSink(path).send("GCash", statement.transactions)
    assert len(_read(path)) == 8


def test_a_dry_run_writes_nothing(tmp_path, statement):
    path = tmp_path / "mm.csv"
    result = MoneyManagerSink(path, dry_run=True).send("GCash", statement.transactions)
    assert len(result.created) == 8
    assert not path.exists()


# --- plain CSV --------------------------------------------------------------


def test_the_csv_carries_signed_amounts_and_the_duplicate_key(tmp_path, statement):
    path = tmp_path / "out.csv"
    CsvSink(path).send("GCash", statement.transactions)
    rows = _read(path)
    assert len(rows) == 8
    assert sum(Decimal(r["amount"]) for r in rows) == (
        statement.total_credit - statement.total_debit
    )
    assert [r["duplicate_key"] for r in rows] == [t.reference_no for t in statement.transactions]


def test_appending_the_same_statement_adds_nothing(tmp_path, statement):
    path = tmp_path / "out.csv"
    CsvSink(path).send("GCash", statement.transactions)
    again = CsvSink(path).send("GCash", statement.transactions)
    assert again.created == []
    assert len(again.duplicates) == 8
    assert len(_read(path)) == 8


def test_the_sink_list_is_fixed():
    """Nothing is discovered from other installed packages."""
    assert available() == ["csv", "moneymanager", "ynab"]
    assert set(SINKS) == {"csv", "moneymanager", "ynab"}
