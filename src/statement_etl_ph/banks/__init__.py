"""The banks and e-wallets this package can read, one module each.

The registry is a fixed list on purpose. Nothing is discovered from other
installed packages: a statement is financial data, and which code reads it
should be decided here, not by whatever else happens to be installed.
"""

from __future__ import annotations

from .base import BankDefinition
from .gcash import BANK as GCASH
from .gotyme import BANK as GOTYME
from .grab import BANK as GRAB

__all__ = ["BankDefinition", "available", "get_bank", "register"]

_REGISTRY: dict[str, BankDefinition] = {bank.name: bank for bank in (GCASH, GOTYME, GRAB)}


def register(bank: BankDefinition) -> None:
    """Add a bank from your own code, e.g. while developing a new parser."""
    _REGISTRY[bank.name] = bank


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_bank(name: str) -> BankDefinition:
    try:
        return _REGISTRY[name.lower()]
    except KeyError:
        raise KeyError(
            f"no bank called {name!r}. Available: {', '.join(available())}"
        ) from None
