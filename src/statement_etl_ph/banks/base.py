"""What a bank is to this package: how to find its mail and how to read it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..models import Statement

# (path, password) -> Statement. A provider whose statement is the email body
# rather than a PDF keeps the same shape and ignores the password.
Parser = Callable[..., Statement]


@dataclass(frozen=True)
class BankDefinition:
    """Everything that differs between banks, in one place.

    Adding a bank means writing one module in this package that defines a
    parser and a ``BANK`` of this type, then listing it in ``banks/__init__``.
    """

    name: str
    # How the provider is named to a person: the Account column of a Money
    # Manager import, a memo, a report.
    display_name: str
    parser: Parser
    # Gmail search for the statement email. The processed label and date
    # window are added at fetch time, not written here.
    gmail_query: str
    # The domains the provider DKIM-signs its mail with. A message no one of
    # them signed is refused, whatever its From header says.
    dkim_domains: tuple[str, ...]
    # The environment variable holding the PDF password. Empty for a provider
    # whose statement is not encrypted.
    password_env: str = ""
    # "pdf" for an attachment; "email_body" when the email *is* the receipt.
    content: str = "pdf"
    # Filename fragments that pick the right PDF when a message carries
    # several, tried in order. A pattern that matches nothing fails the
    # message rather than falling back to the first PDF.
    attachment_patterns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reads_body(self) -> bool:
        return self.content == "email_body"

    @property
    def processed_label(self) -> str:
        return f"statement-etl-ph/{self.name}"
