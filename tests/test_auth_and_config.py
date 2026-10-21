"""Tests for the Gmail token file and the config file."""

from __future__ import annotations

import json

import pytest

from statement_etl_ph.banks import available, get_bank
from statement_etl_ph.config import Config, load_config
from statement_etl_ph.gmail import auth

TOKEN = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
                 "GCASH_PDF_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_the_token_file_is_readable_only_by_its_owner(tmp_path):
    path = tmp_path / "nested" / "token.json"
    auth.save_token(path, TOKEN)
    assert path.stat().st_mode & 0o777 == 0o600


def test_an_existing_loose_file_is_tightened_when_rewritten(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{}")
    path.chmod(0o644)
    auth.save_token(path, TOKEN)
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_token_round_trips_and_defaults_to_the_modify_scope(tmp_path):
    path = tmp_path / "token.json"
    auth.save_token(path, TOKEN)
    loaded = auth.load_token(path)
    assert loaded["refresh_token"] == "refresh"
    assert auth.can_label(loaded)


def test_environment_variables_override_the_file(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    auth.save_token(path, TOKEN)
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "from-env")
    assert auth.load_token(path)["refresh_token"] == "from-env"


def test_environment_variables_alone_are_enough(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    assert auth.load_token(tmp_path / "missing.json")["client_id"] == "id"


def test_a_missing_token_names_the_command_to_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="statement-etl-ph auth"):
        auth.load_token(tmp_path / "missing.json")


def test_a_read_only_token_cannot_label():
    assert not auth.can_label({**TOKEN, "scopes": [auth.SCOPE_READONLY]})


def test_the_default_paths_follow_xdg(tmp_path):
    assert auth.default_token_path() == tmp_path / "config-home" / "statement-etl-ph" / "token.json"


def test_no_config_file_is_an_empty_config():
    assert load_config() == Config()


def test_a_named_config_file_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.json")


def test_password_precedence_is_flag_then_env_then_file(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"banks": {"gcash": {"password": "from-file"}}}))
    config = load_config(path)
    bank = get_bank("gcash")
    assert config.password(bank) == "from-file"
    monkeypatch.setenv("GCASH_PDF_PASSWORD", "from-env")
    assert config.password(bank) == "from-env"
    assert config.password(bank, "from-flag") == "from-flag"


def test_gotyme_can_pick_its_other_statement():
    config = Config(banks={"gotyme": {"attachment_patterns": ["SavingsAccounts"]}})
    assert config.attachment_patterns(get_bank("gotyme")) == ("SavingsAccounts",)
    assert Config().attachment_patterns(get_bank("gotyme")) == ("EverydayDepositAccount",)


# --- bank definitions ---------------------------------------------------------


def test_every_bank_declares_its_dkim_domains_and_a_gmail_search():
    for name in available():
        bank = get_bank(name)
        assert bank.dkim_domains, name
        assert "from:" in bank.gmail_query, name
        assert bank.display_name, name


def test_amazon_ses_is_never_trusted_for_a_bank():
    """GoTyme and Grab send through SES, which signs mail for anyone."""
    for name in available():
        assert "amazonses.com" not in get_bank(name).dkim_domains


def test_bank_names_are_case_insensitive():
    assert get_bank("GCash").name == "gcash"
