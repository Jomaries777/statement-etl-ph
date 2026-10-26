"""Nothing the package or its test tooling prints may include a password.

The fixture passwords are synthetic, but a repository that asks contributors
to guard their own PDF passwords should not show them printing one.
"""

from __future__ import annotations

import make_fixture
import make_gotyme_fixture

from statement_etl_ph import cli


def test_building_the_gcash_fixture_never_prints_its_password(tmp_path, capsys):
    output = make_fixture.build(tmp_path / "statement.pdf")
    printed = capsys.readouterr()
    assert output.exists()
    assert make_fixture.PASSWORD not in printed.out + printed.err


def test_building_the_gotyme_fixture_never_prints_its_password(tmp_path, monkeypatch, capsys):
    # Built in a temporary folder: the committed fixture must stay untouched.
    monkeypatch.setattr(make_gotyme_fixture, "HERE", tmp_path)
    monkeypatch.setattr(make_gotyme_fixture, "OUTPUT", tmp_path / "gotyme.pdf")
    output = make_gotyme_fixture.build()
    printed = capsys.readouterr()
    assert output.exists()
    assert make_gotyme_fixture.PASSWORD not in printed.out + printed.err


def test_auth_with_a_missing_client_file_names_only_the_path(tmp_path, capsys):
    missing = tmp_path / "client_secret.json"
    assert cli.main(["auth", str(missing)]) == 2
    assert capsys.readouterr().err.strip() == f"no such file: {missing}"
