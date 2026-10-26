"""Where parsed transactions go.

Every sink takes the same input — an account name and a list of
:class:`~statement_etl_ph.models.Transaction` — and reports what happened to
each row. Platform details (amount formats, column layouts, duplicate checks)
stay inside the sink that needs them.

The list of sinks is fixed, like the list of banks. Nothing is discovered
from other installed packages, so no other package can register itself as a
sink and receive your transactions.

Categorisation is out of scope everywhere: a sink may pass through a category
the caller supplies, but never decides one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from ..models import Transaction


@dataclass
class SendResult:
    """What a sink did with each row, keyed by the row's duplicate key."""

    created: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    # (duplicate key, why) for rows the sink could not take.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    # Where the rows went: a file path for the file sinks, an account for YNAB.
    location: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed


class Sink(Protocol):
    def send(self, account: str, txns: list[Transaction]) -> SendResult: ...


def _csv(path, **options) -> Sink:
    from .export import CsvSink

    return CsvSink(path, **options)


def _money_manager(path, **options) -> Sink:
    from .money_manager import MoneyManagerSink

    return MoneyManagerSink(path, **options)


def _ynab(path, **options) -> Sink:
    from .ynab import YnabSink

    return YnabSink(**options)


# name -> (factory, file suffix). Each factory takes the output path; a sink
# that writes no file has no suffix and ignores it.
SINKS: dict[str, tuple[Callable[..., Sink], Optional[str]]] = {
    "csv": (_csv, "csv"),
    "moneymanager": (_money_manager, "moneymanager.csv"),
    "ynab": (_ynab, None),
}


def available() -> list[str]:
    return sorted(SINKS)
