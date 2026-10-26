"""Tests for the YNAB sink. No test reaches the network: YNAB is faked."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime
from decimal import Decimal

import pytest

from conftest import FIXTURE, GCASH_PASSWORD
from statement_etl_ph import cli
from statement_etl_ph.models import Transaction
from statement_etl_ph.sinks import ynab
from statement_etl_ph.sinks.ynab import (
    API_BASE,
    YnabClient,
    YnabConverter,
    YnabError,
    YnabFields,
    YnabRateLimited,
    YnabSink,
    build_import_id,
    import_ids,
)

TOKEN = "test-token-not-real"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test may read the developer's own token or config, or call YNAB."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    for name in ("GCASH_PDF_PASSWORD", "GOTYME_PDF_PASSWORD", "YNAB_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def no_network(request, timeout):
        raise AssertionError(f"a test tried to reach {request.full_url}")

    monkeypatch.setattr(ynab, "_urlopen", no_network)


class FakeYnab:
    """Answers like YNAB: an accounts list, and a POST that refuses known import_ids."""

    def __init__(self, accounts=None):
        self.accounts = accounts if accounts is not None else [
            {"id": "acct-gcash", "name": "GCash", "closed": False, "deleted": False},
        ]
        self.imported: set[str] = set()
        self.requests: list[tuple[str, str, dict, object]] = []
        # Answers to give before behaving normally: (status, body) or an exception.
        self.queued: list = []

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append(
            (request.get_method(), request.full_url, dict(request.header_items()), body)
        )
        if self.queued:
            answer = self.queued.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            status, payload = answer
            return status, {}, json.dumps(payload).encode()

        path = request.full_url[len(API_BASE):]
        if request.get_method() == "GET" and path.endswith("/accounts"):
            return 200, {}, json.dumps({"data": {"accounts": self.accounts}}).encode()
        if request.get_method() == "POST" and path.endswith("/transactions"):
            created, duplicates = [], []
            for row in body["transactions"]:
                if row["import_id"] in self.imported:
                    duplicates.append(row["import_id"])
                else:
                    self.imported.add(row["import_id"])
                    created.append({**row, "id": f"txn-{len(self.imported)}"})
            payload = {"data": {"transactions": created, "duplicate_import_ids": duplicates}}
            return 201, {}, json.dumps(payload).encode()
        return 404, {}, json.dumps({"error": {"detail": "Resource not found"}}).encode()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def _txn(desc="Payment to Example Apps", ref="0000100000066", debit=None, credit=None,
         day=26):
    return Transaction(
        posted_at=datetime(2026, 8, day, 9, 9),
        description=desc,
        reference_no=ref,
        debit=Decimal(debit) if debit else None,
        credit=Decimal(credit) if credit else None,
    )


def _client(fake, sleeps=None):
    return YnabClient(TOKEN, transport=fake, sleep=(sleeps.append if sleeps is not None else
                                                    lambda seconds: None))


def _sink(fake, **options):
    options.setdefault("import_id_prefix", "GCASH")
    return YnabSink(client=_client(fake), **options)


# --- import IDs -------------------------------------------------------------


def test_the_import_id_is_the_prefix_and_the_banks_reference():
    assert build_import_id("GCASH", _txn(debit="489.00")) == "GCASH:0000100000066"


def test_the_import_id_format_never_changes():
    """Byte for byte: a different string makes YNAB import every past row again."""
    assert build_import_id("MYGCASH", _txn(debit="489.00")) == "MYGCASH:0000100000066"
    no_ref = _txn(ref="", debit="489.00")
    assert build_import_id("MYGCASH", no_ref) == "YNAB:-489000:2026-08-26:1"
    long_ref = _txn(ref="A-" + "7" * 40, debit="1.00")
    assert len(build_import_id("GRAB", long_ref)) == 36


def test_a_prefix_is_required():
    with pytest.raises(ValueError):
        build_import_id("", _txn(debit="1.00"))
    with pytest.raises(ValueError):
        YnabConverter("")


def test_identical_rows_without_a_reference_are_counted_not_collapsed():
    same = [_txn(ref="", debit="50.00"), _txn(ref="", debit="50.00"), _txn(ref="", debit="50.00",
                                                                             day=27)]
    assert import_ids("GCASH", same) == [
        "YNAB:-50000:2026-08-26:1",
        "YNAB:-50000:2026-08-26:2",
        "YNAB:-50000:2026-08-27:1",
    ]


# --- converting rows --------------------------------------------------------


def test_money_out_is_negative_milliunits_and_money_in_positive():
    convert = YnabConverter("GCASH")
    assert convert.row(_txn(debit="489.00"), "acct", "GCash", "id")["amount"] == -489000
    assert convert.row(_txn(credit="1500.50"), "acct", "GCash", "id")["amount"] == 1500500


def test_a_row_that_moved_no_money_is_not_sent():
    assert YnabConverter("GCASH").row(_txn(), "acct", "GCash", "id") is None


def test_the_payee_is_the_merchant_and_the_memo_the_reference():
    row = YnabConverter("GCASH").row(
        _txn("Payment to Example Apps, Merchant Transaction Number: EXMPL00000A1",
             debit="489.00"),
        "acct", "GCash", "GCASH:0000100000066",
    )
    assert row == {
        "account_id": "acct",
        "date": "2026-08-26",
        "amount": -489000,
        "payee_name": "Example Apps",
        "memo": "ref 0000100000066",
        "cleared": "cleared",
        "approved": False,
        "import_id": "GCASH:0000100000066",
    }


def test_no_category_is_sent_unless_the_caller_decided_one():
    row = YnabConverter("GCASH").row(_txn(debit="5.00"), "acct", "GCash", "id")
    assert "category_id" not in row


def test_a_row_with_no_description_still_gets_a_payee():
    """GCash prints inbound bank transfers with no description."""
    row = YnabConverter("GCASH").row(_txn(desc="", credit="500.00"), "acct", "GCash", "id")
    assert row["payee_name"] == "GCash transaction"


def test_long_payees_are_cut_to_ynabs_limit():
    row = YnabConverter("GCASH").row(_txn(desc="X" * 300, debit="1.00"), "acct", "GCash", "id")
    assert len(row["payee_name"]) == 200


def test_values_the_caller_decided_pass_through_unchanged():
    row = YnabConverter("GCASH").row(
        _txn(debit="489.00"), "acct", "GCash", "id",
        YnabFields(category_id="cat-food", memo="Food · rule", payee_id="payee-transfer"),
    )
    assert row["category_id"] == "cat-food"
    assert row["memo"] == "Food · rule"
    assert row["payee_id"] == "payee-transfer"
    assert "payee_name" not in row  # a payee_id is what makes it a transfer


def test_cleared_and_approved_are_settings():
    row = YnabConverter("GCASH", cleared="uncleared", approved=True).row(
        _txn(debit="1.00"), "acct", "GCash", "id"
    )
    assert (row["cleared"], row["approved"]) == ("uncleared", True)
    with pytest.raises(ValueError):
        YnabConverter("GCASH", cleared="maybe")


# --- the client -------------------------------------------------------------


def test_without_a_token_it_says_how_to_get_one():
    with pytest.raises(YnabError, match="YNAB_ACCESS_TOKEN"):
        YnabClient("")


def test_the_token_goes_in_the_header_and_never_in_repr_or_errors():
    fake = FakeYnab()
    fake.queued = [(401, {"error": {"detail": "Unauthorized"}})]
    client = _client(fake)
    assert TOKEN not in repr(client)
    with pytest.raises(YnabError) as caught:
        client.accounts("last-used")
    assert TOKEN not in str(caught.value)
    assert fake.requests[0][2]["Authorization"] == f"Bearer {TOKEN}"
    assert fake.requests[0][1] == f"{API_BASE}/budgets/last-used/accounts"


def test_a_server_error_is_retried_with_a_growing_wait():
    fake, sleeps = FakeYnab(), []
    fake.queued = [(503, {}), (500, {})]
    assert _client(fake, sleeps).account_id("last-used", "GCash") == "acct-gcash"
    assert sleeps == [2.0, 4.0]
    assert len(fake.requests) == 3


def test_a_network_error_is_retried():
    fake = FakeYnab()
    fake.queued = [urllib.error.URLError("connection reset"), TimeoutError("timed out")]
    assert _client(fake).account_id("last-used", "GCash") == "acct-gcash"


def test_retrying_gives_up_after_four_attempts():
    fake, sleeps = FakeYnab(), []
    fake.queued = [(502, {})] * 4
    with pytest.raises(YnabError, match="502"):
        _client(fake, sleeps).accounts("last-used")
    assert len(fake.requests) == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_the_rate_limit_is_not_retried():
    """YNAB's window is a rolling hour: seconds of retrying learn nothing."""
    fake, sleeps = FakeYnab(), []
    fake.queued = [(429, {"error": {"detail": "Too many requests"}})]
    with pytest.raises(YnabRateLimited, match="an hour"):
        _client(fake, sleeps).accounts("last-used")
    assert len(fake.requests) == 1 and sleeps == []


def test_a_rejected_request_says_what_ynab_said():
    fake = FakeYnab()
    fake.queued = [(400, {"error": {"detail": "import_id is too long"}})]
    with pytest.raises(YnabError, match="import_id is too long"):
        _client(fake).create_transactions("last-used", [{"import_id": "x"}])


def test_closed_and_deleted_accounts_are_ignored():
    fake = FakeYnab([
        {"id": "old", "name": "GCash", "closed": True, "deleted": False},
        {"id": "gone", "name": "GCash", "closed": False, "deleted": True},
        {"id": "live", "name": "GCash", "closed": False, "deleted": False},
    ])
    assert _client(fake).account_id("last-used", "GCash") == "live"


def test_two_open_accounts_with_one_name_is_an_error_not_a_guess():
    fake = FakeYnab([
        {"id": "a", "name": "GCash", "closed": False, "deleted": False},
        {"id": "b", "name": "GCash", "closed": False, "deleted": False},
    ])
    with pytest.raises(YnabError, match="2 open YNAB accounts"):
        _client(fake).account_id("last-used", "GCash")


def test_a_missing_account_lists_the_open_ones():
    fake = FakeYnab([{"id": "a", "name": "My GCash", "closed": False, "deleted": False}])
    with pytest.raises(YnabError, match="open accounts: My GCash"):
        _client(fake).account_id("last-used", "GCash")


def test_accounts_are_read_once_per_client():
    fake = FakeYnab()
    client = _client(fake)
    client.account_id("last-used", "GCash")
    client.account_id("last-used", "GCash")
    assert len(fake.requests) == 1


def test_a_long_statement_is_sent_in_chunks():
    fake = FakeYnab()
    bodies = [{"import_id": f"GCASH:{n}"} for n in range(450)]
    created, duplicates = _client(fake).create_transactions("last-used", bodies)
    assert [len(p[3]["transactions"]) for p in fake.posts()] == [200, 200, 50]
    assert len(created) == 450 and not duplicates


# --- the sink ---------------------------------------------------------------


def test_the_synthetic_statement_goes_in_and_adds_up(statement):
    fake = FakeYnab()
    result = _sink(fake).send("GCash", statement.transactions)
    assert result.ok and len(result.created) == 8
    sent = fake.posts()[0][3]["transactions"]
    assert {r["account_id"] for r in sent} == {"acct-gcash"}
    assert sum(r["amount"] for r in sent) == int(
        (statement.total_credit - statement.total_debit) * 1000
    )
    assert [r["import_id"] for r in sent] == [
        f"GCASH:{t.reference_no}" for t in statement.transactions
    ]


def test_sending_a_statement_again_reports_duplicates_not_new_rows(statement):
    fake = FakeYnab()
    _sink(fake).send("GCash", statement.transactions)
    again = _sink(fake).send("GCash", statement.transactions)
    assert again.created == []
    assert again.duplicates == [t.duplicate_key for t in statement.transactions]
    assert len(fake.imported) == 8


def test_a_dry_run_checks_the_account_and_sends_nothing(statement):
    fake = FakeYnab()
    result = _sink(fake, dry_run=True).send("GCash", statement.transactions)
    assert len(result.created) == 8
    assert [r[0] for r in fake.requests] == ["GET"]


def test_a_configured_account_id_skips_the_lookup():
    fake = FakeYnab()
    result = _sink(fake, account_id="acct-set").send("GCash", [_txn(debit="5.00")])
    assert result.ok
    assert [r[0] for r in fake.requests] == ["POST"]
    assert fake.posts()[0][3]["transactions"][0]["account_id"] == "acct-set"


def test_the_budget_is_in_the_path():
    fake = FakeYnab()
    _sink(fake, budget_id="budget-123").send("GCash", [_txn(debit="5.00")])
    assert all("/budgets/budget-123/" in r[1] for r in fake.requests)


def test_an_unknown_account_fails_every_row_and_sends_nothing():
    fake = FakeYnab([])
    result = _sink(fake).send("GCash", [_txn(debit="5.00"), _txn(ref="0000100000077",
                                                                 debit="6.00")])
    assert not result.ok
    assert [key for key, _ in result.failed] == ["0000100000066", "0000100000077"]
    assert "no open YNAB account" in result.failed[0][1]
    assert fake.posts() == []


def test_a_row_that_moved_no_money_is_skipped_and_reported():
    fake = FakeYnab()
    result = _sink(fake).send("GCash", [_txn(debit="5.00"), _txn(ref="0000100000077")])
    assert result.created == ["0000100000066"]
    assert result.skipped == [("0000100000077", "no debit or credit amount")]


def test_a_failed_post_fails_the_rows_and_says_resending_is_safe():
    fake = FakeYnab()
    fake.queued = [(200, {"data": {"accounts": fake.accounts}}),
                   (400, {"error": {"detail": "bad date"}})]
    result = _sink(fake).send("GCash", [_txn(debit="5.00")])
    assert not result.ok
    assert "bad date" in result.failed[0][1]
    assert "again is safe" in result.failed[0][1]


def test_a_row_ynab_neither_created_nor_called_a_duplicate_is_a_failure():
    fake = FakeYnab()
    fake.queued = [(200, {"data": {"accounts": fake.accounts}}),
                   (201, {"data": {"transactions": [], "duplicate_import_ids": []}})]
    result = _sink(fake).send("GCash", [_txn(debit="5.00")])
    assert result.failed == [("0000100000066",
                              "YNAB accepted the request but did not create this row")]


def test_fields_the_caller_decided_reach_ynab():
    fake = FakeYnab()
    txn = _txn(debit="489.00")
    _sink(fake).send("GCash", [txn], fields={txn: YnabFields(category_id="cat-food")})
    assert fake.posts()[0][3]["transactions"][0]["category_id"] == "cat-food"


# --- the command ------------------------------------------------------------


def _parse_to_ynab(*extra):
    return cli.main([str(FIXTURE), "--bank", "gcash", "--password", GCASH_PASSWORD,
                     "--to", "ynab", *extra])


def test_the_command_without_a_token_says_how_to_get_one(capsys):
    assert _parse_to_ynab() == 2
    assert "YNAB_ACCESS_TOKEN" in capsys.readouterr().err


def test_the_command_imports_the_statement_and_then_finds_duplicates(monkeypatch, capsys):
    fake = FakeYnab()
    monkeypatch.setattr(ynab, "_urlopen", fake)
    monkeypatch.setenv("YNAB_ACCESS_TOKEN", TOKEN)

    assert _parse_to_ynab() == 0
    assert "wrote 8 row(s) to YNAB account 'GCash'" in capsys.readouterr().out
    assert _parse_to_ynab() == 0
    assert "8 already there, not written again" in capsys.readouterr().out
    assert len(fake.imported) == 8
    assert all(i.startswith("GCASH:") for i in fake.imported)


def test_the_command_dry_run_posts_nothing(monkeypatch, capsys):
    fake = FakeYnab()
    monkeypatch.setattr(ynab, "_urlopen", fake)
    monkeypatch.setenv("YNAB_ACCESS_TOKEN", TOKEN)
    assert _parse_to_ynab("--dry-run") == 0
    assert "would write 8 row(s)" in capsys.readouterr().out
    assert fake.posts() == []


def test_the_config_file_supplies_the_token_budget_prefix_and_account(tmp_path, monkeypatch):
    fake = FakeYnab([{"id": "acct-mine", "name": "Wallet", "closed": False, "deleted": False}])
    monkeypatch.setattr(ynab, "_urlopen", fake)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "banks": {"gcash": {"account_name": "Wallet", "import_id_prefix": "MYGCASH"}},
        "ynab": {"token": TOKEN, "budget_id": "budget-9", "approved": True},
    }))
    assert _parse_to_ynab("--config", str(config)) == 0
    sent = fake.posts()[0]
    assert "/budgets/budget-9/transactions" in sent[1]
    assert sent[2]["Authorization"] == f"Bearer {TOKEN}"
    rows = sent[3]["transactions"]
    assert {r["account_id"] for r in rows} == {"acct-mine"}
    assert all(r["import_id"].startswith("MYGCASH:") and r["approved"] for r in rows)


def test_a_failed_import_exits_non_zero(monkeypatch):
    monkeypatch.setattr(ynab, "_urlopen", FakeYnab([]))
    monkeypatch.setenv("YNAB_ACCESS_TOKEN", TOKEN)
    assert _parse_to_ynab() == 1


def test_a_bad_cleared_setting_stops_before_any_request(tmp_path, monkeypatch, capsys):
    fake = FakeYnab()
    monkeypatch.setattr(ynab, "_urlopen", fake)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ynab": {"token": TOKEN, "cleared": "maybe"}}))
    assert _parse_to_ynab("--config", str(config)) == 2
    assert "cleared must be one of" in capsys.readouterr().err
    assert fake.requests == []
