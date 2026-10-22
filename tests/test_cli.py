"""Tests for the statement-etl-ph command."""

from __future__ import annotations

import csv
import json
from datetime import datetime

import pytest

from conftest import FIXTURE, GCASH_PASSWORD
from statement_etl_ph import cli
from statement_etl_ph.gmail.fetch import FetchedStatement


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test may read the developer's own config, token or env passwords."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    for name in ("GCASH_PDF_PASSWORD", "GOTYME_PDF_PASSWORD", "GMAIL_CLIENT_ID",
                 "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_a_file_alone_means_parse(tmp_path, capsys):
    code = cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path)])
    assert code == 0
    written = tmp_path / "gcash_2026-08-22_to_2026-08-28.csv"
    assert len(_rows(written)) == 8
    out = capsys.readouterr().out
    assert "reconcile: ok" in out
    assert "wrote 8 row(s)" in out


def test_the_password_comes_from_the_banks_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("GCASH_PDF_PASSWORD", GCASH_PASSWORD)
    assert cli.main([str(FIXTURE), "--bank", "gcash", "--out", str(tmp_path)]) == 0


def test_a_wrong_password_names_the_variable_to_set(tmp_path, capsys):
    code = cli.main([str(FIXTURE), "--bank", "gcash", "--password", "nope",
                     "--out", str(tmp_path)])
    assert code == 1
    assert "GCASH_PDF_PASSWORD" in capsys.readouterr().err


def test_money_manager_output_is_named_after_the_statement(tmp_path):
    code = cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--to", "moneymanager", "--out", str(tmp_path)])
    assert code == 0
    rows = _rows(tmp_path / "gcash_2026-08-22_to_2026-08-28_moneymanager.csv")
    assert len(rows) == 8
    assert {r["Account"] for r in rows} == {"GCash"}


def test_the_account_name_can_match_one_already_in_the_app(tmp_path):
    out = tmp_path / "mm.csv"
    cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
              "--to", "moneymanager", "--out", str(out), "--account-name", "E-wallet"])
    assert {r["Account"] for r in _rows(out)} == {"E-wallet"}


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    code = cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path), "--dry-run"])
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    assert "would write 8 row(s)" in capsys.readouterr().out


def test_a_statement_that_does_not_reconcile_is_not_written(tmp_path, monkeypatch, capsys):
    from statement_etl_ph.banks import get_bank

    bank = get_bank("gcash")
    real = bank.parser

    def drops_a_row(path, password):
        statement = real(path, password)
        object.__setattr__(statement, "transactions", statement.transactions[:-1])
        return statement

    monkeypatch.setattr(cli, "_parse", lambda b, p, pw: drops_a_row(p, pw))
    code = cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path)])
    assert code == 1
    assert list(tmp_path.iterdir()) == []
    assert "--force" in capsys.readouterr().err

    assert cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path), "--force"]) == 0


def test_the_config_file_supplies_the_password_and_output(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(json.dumps({
        "banks": {"gcash": {"password": GCASH_PASSWORD, "account_name": "Wallet"}},
        "output": {"to": "moneymanager", "dir": str(tmp_path / "out")},
    }))
    assert cli.main([str(FIXTURE), "--bank", "gcash", "--config", str(config)]) == 0
    rows = _rows(tmp_path / "out" / "gcash_2026-08-22_to_2026-08-28_moneymanager.csv")
    assert {r["Account"] for r in rows} == {"Wallet"}


def test_init_writes_a_private_template_with_no_secrets(tmp_path):
    path = tmp_path / "settings.json"
    assert cli.main(["init", "--config", str(path)]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    template = json.loads(path.read_text())
    assert all(not bank.get("password") for bank in template["banks"].values())
    assert cli.main(["init", "--config", str(path)]) == 1, "never overwrites by default"


def test_fetch_without_a_token_says_how_to_get_one(capsys):
    assert cli.main(["fetch", "--bank", "gcash"]) == 2
    assert "statement-etl-ph auth" in capsys.readouterr().err


class _FakeGmail:
    """Stands in for GmailClient: one GCash statement, recording labels."""

    can_label = True

    def __init__(self, token=None):
        self.queries, self.labelled, self.fetch_kwargs = [], [], []

    def find_statements(self, query, max_results=25):
        self.queries.append(query)
        return [{"id": "m1"}]

    def fetch_statement(self, message_id, patterns=(), **kwargs):
        self.fetch_kwargs.append(kwargs)
        return FetchedStatement(
            message_id=message_id, thread_id="t", subject="Your GCash Transaction History",
            received_at=datetime(2026, 8, 29), filename="statement.pdf",
            content=FIXTURE.read_bytes(),
        )

    def mark_processed(self, message_id, label):
        self.labelled.append((message_id, label))


def _fake_gmail(monkeypatch):
    import statement_etl_ph.gmail.auth as auth
    import statement_etl_ph.gmail.fetch as fetch

    gmail = _FakeGmail()
    monkeypatch.setattr(fetch, "GmailClient", lambda token: gmail)
    monkeypatch.setattr(auth, "load_token", lambda path=None: {})
    return gmail


def test_fetch_checks_the_banks_domain_writes_and_labels(tmp_path, monkeypatch):
    gmail = _fake_gmail(monkeypatch)
    code = cli.main(["fetch", "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path), "--since", "all"])
    assert code == 0
    assert "-label:statement-etl-ph-gcash" in gmail.queries[0]
    assert gmail.fetch_kwargs[0]["dkim_domains"] == ("gcash.com",)
    assert gmail.labelled == [("m1", "statement-etl-ph/gcash")]
    assert len(_rows(tmp_path / "gcash_2026-08-22_to_2026-08-28.csv")) == 8


def test_a_dry_run_fetch_labels_nothing(tmp_path, monkeypatch):
    gmail = _fake_gmail(monkeypatch)
    assert cli.main(["fetch", "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--out", str(tmp_path), "--dry-run"]) == 0
    assert gmail.labelled == []


def test_a_fetch_that_cannot_parse_is_not_labelled(tmp_path, monkeypatch):
    gmail = _fake_gmail(monkeypatch)
    assert cli.main(["fetch", "--bank", "gcash", "--password", "wrong",
                     "--out", str(tmp_path)]) == 1
    assert gmail.labelled == []


def _statement_with_an_unprinted_refund():
    """A ₱59 ride GCash charged and refunded, with only the balance showing the refund."""
    from datetime import datetime as dt
    from decimal import Decimal

    from statement_etl_ph.models import Statement, Transaction

    charge = Transaction(dt(2026, 9, 7, 6), "Payment to Sample Rides", "A1",
                         debit=Decimal("59.00"), balance=Decimal("941.00"))
    later = Transaction(dt(2026, 9, 7, 8), "Payment to Sample Bakery", "A2",
                        debit=Decimal("115.00"), balance=Decimal("885.00"))
    return Statement(None, None, Decimal("1000.00"), Decimal("885.00"),
                     None, None, [charge, later])


def test_a_refund_the_statement_never_printed_is_written(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_parse", lambda b, p, pw: _statement_with_an_unprinted_refund())
    out_file = tmp_path / "mm.csv"
    assert cli.main([str(FIXTURE), "--bank", "gcash", "--to", "moneymanager",
                     "--out", str(out_file)]) == 0
    out = capsys.readouterr().out
    assert "+59.00" in out and "Sample Rides (A1)" in out
    assert "written as a refund row" in out
    assert "cover the whole +59.00; your tracker's balance will match" in out

    rows = _rows(out_file)
    assert [(r["Description"], r["Income/Expense"], r["Amount"]) for r in rows] == [
        ("Sample Rides (ref A1)", "Expense", "59.00"),
        ("Sample Rides refund (ref RA1)", "Income", "59.00"),
        ("Sample Bakery (ref A2)", "Expense", "115.00"),
    ]


def test_skip_refunds_leaves_them_out_and_says_what_that_costs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_parse", lambda b, p, pw: _statement_with_an_unprinted_refund())
    out_file = tmp_path / "mm.csv"
    assert cli.main([str(FIXTURE), "--bank", "gcash", "--to", "moneymanager",
                     "--out", str(out_file), "--skip-refunds"]) == 0
    out = capsys.readouterr().out
    assert "not written (--skip-refunds)" in out
    assert "your tracker's balance will differ from the app's by +59.00" in out
    assert len(_rows(out_file)) == 2


def test_a_refund_row_is_a_duplicate_on_the_second_export(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_parse", lambda b, p, pw: _statement_with_an_unprinted_refund())
    out_file = tmp_path / "rows.csv"
    args = [str(FIXTURE), "--bank", "gcash", "--to", "csv", "--out", str(out_file)]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert "3 already there" in capsys.readouterr().out
    assert len(_rows(out_file)) == 3


def test_a_statement_without_a_printed_period_names_its_row_dates(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_parse", lambda b, p, pw: _statement_with_an_unprinted_refund())
    assert cli.main([str(FIXTURE), "--bank", "gcash", "--dry-run"]) == 0
    assert "Period not printed; the rows run 2026-09-07 to 2026-09-07" in capsys.readouterr().out
