"""The optional config file, ``statement-etl-ph.json``.

Everything in it can also come from a flag or an environment variable; the
file only saves typing them. Precedence, highest first: a command-line flag,
the bank's environment variable (``GCASH_PDF_PASSWORD``, ...), the file.

The file holds PDF passwords and can hold a YNAB token, so ``init`` creates it
readable only by you, the same as the Gmail token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .gmail.auth import config_dir, write_private

# Read here rather than from sinks.ynab, so loading a config never imports a sink.
YNAB_TOKEN_ENV = "YNAB_ACCESS_TOKEN"

TEMPLATE = {
    "_comment": (
        "statement-etl-ph settings. Every value can also be a command-line flag; "
        "passwords can be environment variables instead (GCASH_PDF_PASSWORD, "
        "GOTYME_PDF_PASSWORD, YNAB_ACCESS_TOKEN). Delete the banks you do not use."
    ),
    "banks": {
        "gcash": {"password": "", "account_name": "GCash"},
        "gotyme": {
            "password": "",
            "account_name": "GoTyme",
            "attachment_patterns": ["EverydayDepositAccount"],
        },
        "grab": {"account_name": "GrabPay"},
    },
    "output": {"to": "moneymanager", "dir": "~/Statements", "date_format": "%Y-%m-%d"},
    "gmail": {"since": "30d", "timezone": "Asia/Manila", "max_messages": 25},
    "ynab": {"token": "", "budget_id": "last-used", "cleared": "cleared", "approved": False},
}


def default_config_path() -> Path:
    return config_dir() / "statement-etl-ph.json"


@dataclass
class Config:
    banks: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    gmail: dict = field(default_factory=dict)
    ynab: dict = field(default_factory=dict)

    def bank(self, name: str) -> dict:
        return self.banks.get(name) or {}

    def password(self, bank, flag: Optional[str] = None) -> str:
        if flag:
            return flag
        if bank.password_env and os.environ.get(bank.password_env):
            return os.environ[bank.password_env]
        return self.bank(bank.name).get("password", "")

    def account_name(self, bank, flag: Optional[str] = None) -> str:
        return flag or self.bank(bank.name).get("account_name") or bank.display_name

    def attachment_patterns(self, bank) -> tuple[str, ...]:
        configured = self.bank(bank.name).get("attachment_patterns")
        return tuple(configured) if configured else bank.attachment_patterns

    def ynab_token(self) -> str:
        return os.environ.get(YNAB_TOKEN_ENV) or self.ynab.get("token", "")

    def import_id_prefix(self, bank) -> str:
        """The start of every YNAB import_id for this bank: ``GCASH:<reference>``.

        Set it once. Changing it gives every past row a new import_id, and
        YNAB would import the whole history again.
        """
        return self.bank(bank.name).get("import_id_prefix") or bank.name.upper()


def load_config(path: Optional[Path] = None) -> Config:
    """The config file, or an empty config when there is none.

    Only a file named explicitly must exist; the default location is optional.
    """
    explicit = path is not None
    path = Path(path).expanduser() if path else default_config_path()
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"no config file at {path}")
        return Config()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Config(
        banks=raw.get("banks") or {},
        output=raw.get("output") or {},
        gmail=raw.get("gmail") or {},
        ynab=raw.get("ynab") or {},
    )


def write_template(path: Path, *, force: bool = False) -> Path:
    path = Path(path).expanduser()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to replace it")
    write_private(path, json.dumps(TEMPLATE, indent=2) + "\n")
    return path
