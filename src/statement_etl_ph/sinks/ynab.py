"""Send transactions straight into a YNAB budget, through YNAB's own API.

Transaction -> :class:`YnabConverter` -> :class:`YnabClient` -> YNAB.

* **Amount** is YNAB's signed milliunits: money out is negative.
* **Payee** is the merchant, with the same fallback as the Money Manager
  sink for a row that printed no description.
* **Memo** is the bank's reference number, so every row in YNAB can be traced
  back to its statement line.
* **Category** is left empty: this package does not categorise, and YNAB's
  own payee rules, or you, decide it after the import.
* **import_id** is built from the bank's reference number with a prefix.
  YNAB refuses a second transaction with an import_id it has already seen on
  that account, so sending the same statement twice reports the rows as
  duplicates instead of doubling them. The guarantee lives on YNAB's server,
  so no local bookkeeping is needed.

The API is called with the standard library only, so this sink needs no
extra install. The token goes in the ``Authorization`` header and nowhere
else: it is never logged, printed, or put in an error message.

Code that categorises its own rows can still use this module: the converter
takes a category, memo or payee the caller has already decided, per row (see
:class:`YnabFields`), and passes it through unchanged.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional

from ..models import Transaction
from . import SendResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.ynab.com/v1"
TOKEN_ENV = "YNAB_ACCESS_TOKEN"
# YNAB's own shorthand for the budget opened most recently in the app.
LAST_USED = "last-used"

IMPORT_ID_MAX = 36
PAYEE_MAX = 200
MEMO_MAX = 200
CLEARED_STATES = ("cleared", "uncleared", "reconciled")
# One POST carries a whole statement; this only bounds an unusually long one.
BULK_CHUNK = 200

# Attempts at a request YNAB failed on its side, and the first wait between
# them (doubling each time: 2s, 4s, 8s).
ATTEMPTS = 4
FIRST_WAIT = 2.0
TIMEOUT = 45


class YnabError(RuntimeError):
    pass


class RetryableYnabError(YnabError):
    """A fault on YNAB's side or in the network: worth trying again shortly."""


class YnabRateLimited(YnabError):
    """The 200-requests-per-hour budget is spent.

    Deliberately not a :class:`RetryableYnabError`. YNAB's window is a rolling
    hour, so retrying for a few seconds learns nothing; nothing was imported,
    and sending the statement again later is safe.
    """


# --- import IDs ---------------------------------------------------------------


def build_import_id(prefix: str, txn: Transaction, occurrence: int = 1) -> str:
    """The key YNAB uses to refuse a row it has already imported.

    ``<prefix>:<reference>`` when the bank printed a reference. Without one,
    YNAB's documented ``YNAB:<milliunits>:<date>:<occurrence>`` convention,
    where the occurrence counts identical amounts on the same day, so two
    equal payments on one date are both kept.

    Never change the prefix of an account you have already imported into:
    every row would get a new import_id, and YNAB would import the whole
    history again as new transactions.
    """
    if not prefix:
        raise ValueError("an import_id prefix is required, e.g. the bank's name")
    if txn.reference_no:
        candidate = f"{prefix}:{txn.reference_no}"
    else:
        candidate = f"YNAB:{txn.milliunits}:{txn.value_date.isoformat()}:{occurrence}"
    return candidate[:IMPORT_ID_MAX]


def import_ids(prefix: str, txns: Iterable[Transaction]) -> list[str]:
    """One import_id per row, counting occurrences for rows with no reference."""
    seen: Counter = Counter()
    ids = []
    for txn in txns:
        occurrence = 1
        if not txn.reference_no:
            seen[(txn.milliunits, txn.value_date)] += 1
            occurrence = seen[(txn.milliunits, txn.value_date)]
        ids.append(build_import_id(prefix, txn, occurrence))
    return ids


# --- converting rows ----------------------------------------------------------


@dataclass(frozen=True)
class YnabFields:
    """Values the caller has already decided for one row.

    The package never decides any of these; it only passes them through.
    ``payee_id`` takes precedence over ``payee_name``: passing an account's
    ``Transfer : <account>`` payee is how YNAB records a transfer.
    """

    category_id: Optional[str] = None
    memo: Optional[str] = None
    payee_name: Optional[str] = None
    payee_id: Optional[str] = None


def payee_name(txn: Transaction, account: str) -> str:
    """The merchant, or a name for a row that printed no description."""
    name = txn.merchant or " ".join(txn.description.split()) or f"{account} transaction"
    return name[:PAYEE_MAX]


class YnabConverter:
    """Pure: turns transactions into YNAB request bodies, calling nothing."""

    def __init__(self, import_id_prefix: str, *, cleared: str = "cleared",
                 approved: bool = False):
        if not import_id_prefix:
            raise ValueError("an import_id prefix is required, e.g. the bank's name")
        if cleared not in CLEARED_STATES:
            raise ValueError(f"cleared must be one of {', '.join(CLEARED_STATES)}")
        self.import_id_prefix = import_id_prefix
        self.cleared = cleared
        self.approved = approved

    def row(self, txn: Transaction, account_id: str, account: str, import_id: str,
            fields: Optional[YnabFields] = None) -> Optional[dict]:
        """One YNAB transaction, or ``None`` for a row that moved no money."""
        if not txn.milliunits:
            return None
        fields = fields or YnabFields()
        body = {
            "account_id": account_id,
            "date": txn.value_date.isoformat(),
            "amount": txn.milliunits,
            "memo": (fields.memo if fields.memo is not None else self.memo(txn))[:MEMO_MAX],
            "cleared": self.cleared,
            "approved": self.approved,
            "import_id": import_id,
        }
        if fields.payee_id:
            body["payee_id"] = fields.payee_id
        else:
            body["payee_name"] = (fields.payee_name or payee_name(txn, account))[:PAYEE_MAX]
        if fields.category_id:
            body["category_id"] = fields.category_id
        return body

    @staticmethod
    def memo(txn: Transaction) -> str:
        return f"ref {txn.reference_no}" if txn.reference_no else ""

    def rows(
        self,
        txns: list[Transaction],
        account_id: str,
        account: str,
        fields: Optional[Mapping[Transaction, YnabFields]] = None,
    ) -> list[tuple[Transaction, Optional[dict]]]:
        """Every row with its body, ``None`` where it moved no money."""
        fields = fields or {}
        ids = import_ids(self.import_id_prefix, txns)
        return [
            (txn, self.row(txn, account_id, account, import_id, fields.get(txn)))
            for txn, import_id in zip(txns, ids)
        ]


# --- talking to YNAB ----------------------------------------------------------

# (request, timeout) -> (status, headers, body). Swapped for a fake in tests.
Transport = Callable[[urllib.request.Request, float], tuple[int, Mapping[str, str], bytes]]


def _urlopen(request: urllib.request.Request, timeout: float):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers or {}, exc.read() or b""


class YnabClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise YnabError(
                f"no YNAB access token. Create one in YNAB under Account Settings -> "
                f"Developer Settings, then set {TOKEN_ENV}."
            )
        self._token = token
        self._transport = transport or _urlopen
        self._sleep = sleep
        self._accounts: dict[str, list[dict]] = {}

    def __repr__(self) -> str:
        return "YnabClient(token=<hidden>)"

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """One API call, retried only when YNAB or the network failed on its side."""
        data = json.dumps(body).encode() if body is not None else None
        wait = FIRST_WAIT
        attempt = 1
        while True:
            request = urllib.request.Request(
                f"{API_BASE}{path}",
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            try:
                return self._answer(method, path, *self._call(request))
            except RetryableYnabError as exc:
                if attempt == ATTEMPTS:
                    raise
                logger.debug("%s %s failed (%s); retrying in %.0fs", method, path, exc, wait)
                self._sleep(wait)
                wait *= 2
                attempt += 1

    def _call(self, request: urllib.request.Request) -> tuple[int, Mapping[str, str], bytes]:
        try:
            return self._transport(request, TIMEOUT)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise RetryableYnabError(f"could not reach YNAB: {reason}") from exc

    @staticmethod
    def _answer(method: str, path: str, status: int, headers, raw: bytes) -> dict:
        text = raw.decode("utf-8", "replace")
        if status == 429:
            raise YnabRateLimited(
                "YNAB's limit of 200 requests an hour is used up. Nothing was "
                "imported; try again in an hour."
            )
        if status == 401:
            raise YnabError(f"YNAB refused the access token. Check {TOKEN_ENV}.")
        if status >= 500:
            raise RetryableYnabError(f"YNAB returned {status} for {method} {path}")
        if status >= 400:
            try:
                detail = json.loads(text)["error"]["detail"]
            except (ValueError, KeyError, TypeError):
                detail = text[:300]
            raise YnabError(f"YNAB returned {status} for {method} {path}: {detail}")
        try:
            return json.loads(text)
        except ValueError:
            raise YnabError(f"YNAB sent an answer that is not JSON for {method} {path}") from None

    def accounts(self, budget_id: str) -> list[dict]:
        """The budget's accounts, read once per client."""
        if budget_id not in self._accounts:
            data = self._request("GET", f"/budgets/{budget_id}/accounts")
            self._accounts[budget_id] = data["data"]["accounts"]
        return self._accounts[budget_id]

    def account_id(self, budget_id: str, name: str) -> str:
        """The ID of the one open account with exactly this name."""
        open_accounts = [
            a for a in self.accounts(budget_id) if not a.get("closed") and not a.get("deleted")
        ]
        matches = [a for a in open_accounts if a.get("name", "").strip() == name.strip()]
        if len(matches) == 1:
            return matches[0]["id"]
        if matches:
            raise YnabError(
                f"{len(matches)} open YNAB accounts are named {name!r}; rename one, "
                "or set the account's ID in the config file"
            )
        names = ", ".join(sorted(a.get("name", "") for a in open_accounts)) or "none"
        raise YnabError(
            f"no open YNAB account is named {name!r} (open accounts: {names}). "
            "Use --account-name to pick one."
        )

    def create_transactions(self, budget_id: str, bodies: list[dict]) -> tuple[set[str], set[str]]:
        """POST the rows. Returns the import_ids created and those YNAB already had."""
        created: set[str] = set()
        duplicates: set[str] = set()
        for start in range(0, len(bodies), BULK_CHUNK):
            data = self._request(
                "POST",
                f"/budgets/{budget_id}/transactions",
                {"transactions": bodies[start:start + BULK_CHUNK]},
            )["data"]
            created.update(t.get("import_id") for t in data.get("transactions") or [])
            duplicates.update(data.get("duplicate_import_ids") or [])
        return created, duplicates


# --- the sink -----------------------------------------------------------------


class YnabSink:
    def __init__(
        self,
        *,
        import_id_prefix: str,
        token: str = "",
        budget_id: str = LAST_USED,
        account_id: Optional[str] = None,
        cleared: str = "cleared",
        approved: bool = False,
        dry_run: bool = False,
        client: Optional[YnabClient] = None,
    ):
        self.converter = YnabConverter(import_id_prefix, cleared=cleared, approved=approved)
        self.client = client or YnabClient(token)
        self.budget_id = budget_id or LAST_USED
        self.account_id = account_id
        self.dry_run = dry_run

    def send(
        self,
        account: str,
        txns: list[Transaction],
        fields: Optional[Mapping[Transaction, YnabFields]] = None,
    ) -> SendResult:
        result = SendResult(location=f"YNAB account {account!r}")
        try:
            # Read-only, so a dry run makes it too: it proves the token,
            # budget and account are right before anything is sent.
            account_id = self.account_id or self.client.account_id(self.budget_id, account)
        except YnabError as exc:
            result.failed = [(t.duplicate_key, str(exc)) for t in txns]
            return result

        pending: list[tuple[str, dict]] = []
        for txn, body in self.converter.rows(txns, account_id, account, fields):
            if body is None:
                result.skipped.append((txn.duplicate_key, "no debit or credit amount"))
            else:
                pending.append((txn.duplicate_key, body))

        if self.dry_run or not pending:
            result.created = [key for key, _ in pending]
            return result

        try:
            created, duplicates = self.client.create_transactions(
                self.budget_id, [body for _, body in pending]
            )
        except YnabError as exc:
            # A long statement goes in several POSTs, and earlier ones may
            # have landed. Their import_ids make sending it again safe.
            why = f"{exc} Sending the statement again is safe: rows YNAB has come back as duplicates."
            result.failed = [(key, why) for key, _ in pending]
            return result

        for key, body in pending:
            if body["import_id"] in duplicates:
                result.duplicates.append(key)
            elif body["import_id"] in created:
                result.created.append(key)
            else:
                result.failed.append((key, "YNAB accepted the request but did not create this row"))
        return result
