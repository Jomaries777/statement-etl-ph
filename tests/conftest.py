"""Shared fixtures: the synthetic statements, generated on first use.

Every statement here is synthetic. Never add a real one: see CONTRIBUTING.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
# The generators are scripts, not a package; tests import their row tables.
sys.path.insert(0, str(FIXTURES))

FIXTURE = FIXTURES / "synthetic_statement.pdf"
GCASH_PASSWORD = "testuser0000"
GOTYME_FIXTURE = FIXTURES / "synthetic_gotyme_statement.pdf"
GOTYME_PASSWORD = "01011990"


@pytest.fixture(scope="session")
def statement():
    from make_fixture import build

    from statement_etl_ph.banks.gcash import parse_statement

    if not FIXTURE.exists():
        build()
    return parse_statement(FIXTURE, GCASH_PASSWORD)


@pytest.fixture(scope="session")
def gotyme_statement():
    from make_gotyme_fixture import build

    from statement_etl_ph.banks.gotyme import parse_statement

    if not GOTYME_FIXTURE.exists():
        build()
    return parse_statement(GOTYME_FIXTURE, GOTYME_PASSWORD)
