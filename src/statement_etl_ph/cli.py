"""The ``statement-etl-ph`` command.

    statement-etl-ph statement.pdf --bank gcash --to moneymanager
    statement-etl-ph fetch --bank gcash --to csv --out ~/Statements/
    statement-etl-ph auth client_secret.json
    statement-etl-ph init

A file on its own is parsed; ``parse`` is the subcommand it implies.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Optional

from . import __version__
from .banks import available as available_banks
from .banks import get_bank
from .banks.base import BankDefinition
from .config import Config, default_config_path, load_config, write_template
from .models import Statement, StatementParseError, Transaction
from .sinks import SINKS, SendResult
from .sinks import available as available_sinks

logger = logging.getLogger("statement_etl_ph")

_SUBCOMMANDS = ("parse", "fetch", "auth", "init")


# --- printing -----------------------------------------------------------------


def _print_statement(statement: Statement) -> None:
    days = [t.value_date for t in statement.transactions]
    if statement.period_start and statement.period_end:
        period = f"Period {statement.period_start} to {statement.period_end}"
    elif days:
        period = f"Period not printed; the rows run {min(days)} to {max(days)}"
    else:
        period = "Period not printed"
    print(f"{period}: {len(statement.transactions)} transaction(s)")
    for txn in statement.transactions:
        amount = f"{txn.signed_amount:+,.2f}"
        balance = "" if txn.balance is None else f"{txn.balance:,.2f}"
        print(
            f"  {txn.posted_at:%Y-%m-%d %H:%M}  {amount:>12}  {balance:>12}  "
            f"{txn.reference_no:<16}  {txn.merchant or txn.description}"
        )
    for note in statement.notes:
        print(f"  note: {note}")


def _refund_rows(statement: Statement) -> list[Transaction]:
    """Refunds only the balance column shows, as rows a tracker can import.

    Each one exactly reverses a printed charge (see ``RecoveredRow``), so
    writing it is what keeps the tracker's balance equal to the app's. It is
    named after the charge, and its reference is the charge's with an ``R``
    in front, so re-importing the statement finds it as a duplicate.
    """
    return [
        dataclasses.replace(
            row.as_transaction(), description=f"{row.merchant or 'Charge'} refund"
        )
        for row in statement.recover_unprinted_rows()
    ]


def _balance_gap(statement: Statement) -> Optional[Decimal]:
    """How far the printed ending balance is from what the rows add up to."""
    if statement.starting_balance is None or statement.ending_balance is None:
        return None
    moved = sum((t.signed_amount for t in statement.transactions), Decimal("0"))
    return statement.ending_balance - (statement.starting_balance + moved)


def _print_reconciliation(statement: Statement, write_refunds: bool) -> bool:
    result = statement.reconcile()
    if result.ok:
        print("reconcile: ok (the rows add up to the totals the statement prints)")
    for error in result.errors:
        print(f"reconcile: ERROR {error}")
    for warning in result.warnings:
        print(f"reconcile: warning {warning}")
    recovered = statement.recover_unprinted_rows()
    fate = "written as a refund row" if write_refunds else "not written (--skip-refunds)"
    for row in recovered:
        print(
            f"reconcile: note the balance shows {row.amount:+,.2f} on "
            f"{row.posted_at:%Y-%m-%d} reversing {row.merchant or 'a charge'} "
            f"({row.offsets.reference_no}), with no row printed; {fate}"
        )
    for gap in statement.unexplained_gaps():
        print(f"reconcile: warning {gap.describes}")

    gap = _balance_gap(statement)
    if gap:
        explained = sum((r.amount for r in recovered), Decimal("0")) if write_refunds else 0
        left = gap - explained
        if explained and not left:
            print(
                f"reconcile: ok the {len(recovered)} refund row(s) cover the whole "
                f"{gap:+,.2f}; your tracker's balance will match the app's"
            )
        else:
            print(
                f"reconcile: warning your tracker's balance will differ from the app's "
                f"by {left:+,.2f}"
            )
    return result.ok


def _print_result(result: SendResult, dry_run: bool) -> None:
    verb = "would write" if dry_run else "wrote"
    where = f" to {result.location}" if result.location else ""
    print(f"{verb} {len(result.created)} row(s){where}")
    if result.duplicates:
        print(f"  {len(result.duplicates)} already there, not written again")
    for key, why in result.skipped:
        print(f"  skipped {key}: {why}")
    for key, why in result.failed:
        print(f"  FAILED {key}: {why}")


# --- shared steps -------------------------------------------------------------


def _is_password_error(exc: BaseException) -> bool:
    """pdfplumber wraps pdfminer's PDFPasswordIncorrect, so look through the chain."""
    pending, visited = [exc], set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if "password" in type(current).__name__.lower():
            return True
        pending.extend(a for a in current.args if isinstance(a, BaseException))
        pending.extend(c for c in (current.__cause__, current.__context__) if c is not None)
    return False


def _parse(bank: BankDefinition, path: Path, password: str) -> Statement:
    try:
        return bank.parser(path, password)
    except StatementParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - pdfminer has no common base
        if _is_password_error(exc):
            hint = f" Set {bank.password_env} or pass --password." if bank.password_env else ""
            raise StatementParseError(f"wrong or missing PDF password.{hint}") from exc
        raise StatementParseError(f"could not read {path.name}: {exc}") from exc


def _output_path(
    out: Optional[str], config: Config, bank: BankDefinition, statement: Statement, suffix: str
) -> Path:
    """Where a statement's file goes: ``--out`` as a file, or a named file in a folder."""
    if out and out.lower().endswith(".csv"):
        return Path(out).expanduser()
    folder = Path(out or config.output.get("dir") or ".").expanduser()
    days = [t.value_date for t in statement.transactions]
    start = statement.period_start or (min(days) if days else "undated")
    end = statement.period_end or (max(days) if days else "undated")
    stem = f"{bank.name}_{start}_to_{end}"
    if bank.reads_body and statement.transactions:
        # One receipt per email: several can share a day, never a reference.
        stem += f"_{statement.transactions[0].reference_no}"
    return folder / f"{stem}_{suffix}" if suffix != "csv" else folder / f"{stem}.csv"


def _deliver(
    statement: Statement,
    bank: BankDefinition,
    args: argparse.Namespace,
    config: Config,
) -> int:
    """Print, check, and hand the rows to the sink. Returns an exit code."""
    _print_statement(statement)
    write_refunds = not args.skip_refunds
    if not _print_reconciliation(statement, write_refunds) and not args.force:
        print(
            "Nothing written: the rows do not match the statement's own totals, so "
            "something was misread. --force writes them anyway.",
            file=sys.stderr,
        )
        return 1
    rows = list(statement.transactions)
    if write_refunds:
        rows = sorted(rows + _refund_rows(statement), key=lambda t: t.posted_at)
    if not rows:
        return 0

    to = args.to or config.output.get("to") or "csv"
    factory, suffix = SINKS[to]
    options: dict = {"dry_run": args.dry_run}
    if to == "moneymanager":
        options["date_format"] = args.date_format or config.output.get("date_format") or "%Y-%m-%d"
    sink = factory(_output_path(args.out, config, bank, statement, suffix), **options)
    result = sink.send(config.account_name(bank, args.account_name), rows)
    _print_result(result, args.dry_run)
    return 0 if result.ok else 1


# --- commands -----------------------------------------------------------------


def cmd_parse(args: argparse.Namespace, config: Config) -> int:
    bank = get_bank(args.bank)
    path = Path(args.file).expanduser()
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    try:
        statement = _parse(bank, path, config.password(bank, args.password))
    except StatementParseError as exc:
        print(f"{bank.display_name}: {exc}", file=sys.stderr)
        return 1
    return _deliver(statement, bank, args, config)


def cmd_fetch(args: argparse.Namespace, config: Config) -> int:
    from .gmail.auth import load_token
    from .gmail.fetch import GmailClient, build_gmail_query

    names = args.bank or list(config.banks)
    if not names:
        print("name a bank with --bank (or list some in the config file)", file=sys.stderr)
        return 2
    try:
        gmail = GmailClient(load_token(Path(args.token) if args.token else None))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2

    since = args.since or config.gmail.get("since") or "30d"
    tz = config.gmail.get("timezone") or "Asia/Manila"
    limit = int(config.gmail.get("max_messages") or 25)
    status = 0

    for name in names:
        bank = get_bank(name)
        base = bank.gmail_query
        if gmail.can_label:
            # Gmail search writes a nested label's "/" as "-".
            base += f" -label:{bank.processed_label.replace('/', '-')}"
        query = build_gmail_query(base, since, tz)
        print(f"\n== {bank.display_name}: searching Gmail for {query}")
        for stub in gmail.find_statements(query, max_results=limit):
            fetched = gmail.fetch_statement(
                stub["id"],
                config.attachment_patterns(bank),
                body_only=bank.reads_body,
                dkim_domains=bank.dkim_domains,
            )
            if fetched is None:
                status = 1
                continue
            print(f"\n-- {fetched.subject} ({fetched.received_at:%Y-%m-%d})")
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / Path(fetched.filename).name
                path.write_bytes(fetched.content)
                try:
                    statement = _parse(bank, path, config.password(bank, args.password))
                except StatementParseError as exc:
                    print(f"{bank.display_name}: {exc}", file=sys.stderr)
                    status = 1
                    continue
            code = _deliver(statement, bank, args, config)
            if code == 0 and not args.dry_run:
                gmail.mark_processed(fetched.message_id, bank.processed_label)
            status = status or code
    return status


def cmd_auth(args: argparse.Namespace, config: Config) -> int:
    from .gmail.auth import default_token_path, run_consent_flow, save_token

    secrets = Path(args.client_secrets).expanduser()
    if not secrets.exists():
        print(f"no such file: {secrets}", file=sys.stderr)
        return 2
    token = run_consent_flow(secrets, readonly=args.readonly)
    path = Path(args.token).expanduser() if args.token else default_token_path()
    save_token(path, token)
    scope = "read-only" if args.readonly else "read and label (gmail.modify)"
    print(f"Saved the Gmail token to {path} (readable only by you). Access: {scope}.")
    print("Revoke it any time at https://myaccount.google.com/permissions")
    return 0


def cmd_init(args: argparse.Namespace, config: Config) -> int:
    try:
        path = write_template(Path(args.config) if args.config else default_config_path(),
                              force=args.force)
    except FileExistsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Wrote {path} (readable only by you).")
    print("Fill in your PDF passwords there, or set GCASH_PDF_PASSWORD / GOTYME_PDF_PASSWORD.")
    return 0


# --- argument parsing ---------------------------------------------------------


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", choices=available_sinks(),
                        help="output format (default: the config file's, else csv)")
    parser.add_argument("--out", help="output folder, or a .csv file path")
    parser.add_argument("--password", help="PDF password (default: the bank's env variable)")
    parser.add_argument("--account-name", help="the Account column value (default: the bank's name)")
    parser.add_argument("--date-format", help="Money Manager date format (default: %%Y-%%m-%%d)")
    parser.add_argument("--dry-run", action="store_true", help="print the rows; write nothing")
    parser.add_argument("--skip-refunds", action="store_true",
                        help="leave out refunds that only the running balance shows")
    parser.add_argument("--force", action="store_true",
                        help="write even when the rows do not reconcile with the printed totals")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v shows how the PDF was read, column positions included")
    common.add_argument("--config", help=f"config file (default: {default_config_path()})")

    parser = argparse.ArgumentParser(
        prog="statement-etl-ph",
        description="Read GCash, GoTyme and Grab statements into a CSV or a Money Manager import.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", parents=[common], help="read a statement file you already have")
    p.add_argument("file")
    p.add_argument("--bank", required=True, choices=available_banks())
    _add_output_options(p)
    p.set_defaults(func=cmd_parse)

    f = sub.add_parser("fetch", parents=[common],
                       help="find statements in Gmail and read them (needs [gmail])")
    f.add_argument("--bank", action="append", choices=available_banks(),
                   help="repeat for several (default: every bank in the config file)")
    f.add_argument("--since", help="how far back: today, yesterday, 30d, 2w, 3m, all")
    f.add_argument("--token", help="Gmail token file (default: the one `auth` saved)")
    _add_output_options(f)
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("auth", parents=[common],
                       help="sign in to Gmail once and save the token (needs [gmail])")
    a.add_argument("client_secrets", help="the client_secret.json of your Desktop OAuth client")
    a.add_argument("--readonly", action="store_true",
                   help="read-only access: processed messages are not labelled")
    a.add_argument("--token", help="where to save the token")
    a.set_defaults(func=cmd_auth)

    i = sub.add_parser("init", parents=[common], help="write a config file template")
    i.add_argument("--force", action="store_true", help="replace an existing file")
    i.set_defaults(func=cmd_init)
    return parser


def _with_implied_parse(argv: list[str]) -> list[str]:
    """``statement-etl-ph FILE ...`` means ``statement-etl-ph parse FILE ...``."""
    if argv and not argv[0].startswith("-") and argv[0] not in _SUBCOMMANDS:
        return ["parse", *argv]
    return argv


def main(argv: Optional[list[str]] = None) -> int:
    argv = _with_implied_parse(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        # init creates the file, so it must not insist on reading it first.
        config = (
            Config() if args.func is cmd_init
            else load_config(Path(args.config) if args.config else None)
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args, config)
    except ImportError as exc:
        print(
            f"{exc.name or 'a dependency'} is not installed. Gmail needs the extra: "
            'pip install "statement-etl-ph[gmail]"',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
