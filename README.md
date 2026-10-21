# statement-etl-ph

Turn your **GCash**, **GoTyme** and **GrabPay** statements into a list of transactions you can actually use: a CSV for a spreadsheet, or a file you import straight into the **Money Manager** app.

Every statement is checked before anything is written. The rows it reads must add up to the totals the bank printed, so a misread statement stops instead of quietly putting wrong numbers into your budget.

> **Only trust the official source.** The package is `statement-etl-ph` on PyPI and the code lives at <https://github.com/Jomaries777/statement-etl-ph>. This tool runs on your own computer. It never sends your statements, passwords or transactions to anyone. Be suspicious of any copy, fork or "helper" site that asks for them.

## Quick start: one statement, no setup

You need Python 3.10 or newer.

1. **Get your statement.** In the GCash app, open your transaction history and request it by email. It arrives as a password-protected PDF; the email tells you what the password is.
2. **Install:**
   ```bash
   pipx install statement-etl-ph
   ```
   (Or `pip install statement-etl-ph` inside a virtual environment.)
3. **Convert it:**
   ```bash
   statement-etl-ph statement.pdf --bank gcash --to moneymanager --password YOUR-PDF-PASSWORD
   ```
   It prints every row and the check, then writes `gcash_<from>_to_<to>_moneymanager.csv`. To keep the password out of your shell history, set `GCASH_PDF_PASSWORD` instead of passing `--password`.
4. **Import that file into Money Manager.** The Category column is left blank on purpose, so you can sort rows in the app.

Add `--dry-run` to see the rows without writing anything, and `--to csv` for a plain spreadsheet instead.

### What the check catches

GCash prints some inbound bank transfers with **no description at all**. A parser that matches a list of descriptions to a list of amounts by position shifts every later amount onto the wrong row, then drops the last one, and reports no error. This package reads the page as a grid, keeps the blank row, and then proves nothing moved: the debits and credits it read must equal the statement's printed Total Debit and Total Credit. If they don't, nothing is written (unless you pass `--force`).

It also reports what the bank's own arithmetic leaves out. GCash sometimes refunds a cancelled booking without printing a row, and the running balance is the only evidence. Such refunds, and any other gap the balance proves but nothing explains, are listed in the output rather than hidden. They're never written as rows: add them yourself if you want them.

## Supported banks and e-wallets

| `--bank` | What it reads | Password variable |
|---|---|---|
| `gcash` | GCash transaction history PDF | `GCASH_PDF_PASSWORD` |
| `gotyme` | GoTyme Bank statement of account PDF (the Everyday deposit account by default) | `GOTYME_PDF_PASSWORD` |
| `grab` | Grab e-receipt emails; only GrabPay Wallet payments are imported, because card and GCash payments already appear on those statements | none |

Want another bank? See [Adding a bank](docs/add-a-bank.md). It's one file.

## Output formats

**`--to moneymanager`**: Money Manager's bulk import, with these columns:

| Column | Value |
|---|---|
| Date | the transaction date on the statement (default `2026-08-26`; change it with `--date-format`) |
| Account | `GCash`, `GoTyme` or `GrabPay`; use `--account-name` to match an account you already have |
| Category, Subcategory, Note | blank |
| Amount | always positive, e.g. `1250.00` |
| Income/Expense | `Income` for money in, `Expense` for money out |
| Description | the merchant and the bank's reference, e.g. `Example Apps (ref 0000100000066)` |

Money Manager doesn't detect duplicates, so importing the same file twice imports every row twice. Each file is named after its bank and statement period to make that easy to notice.

**`--to csv`**: one row per transaction with the signed amount, debit, credit, balance, reference number and a `duplicate_key` column. Writing into a CSV that already exists only appends rows whose key isn't there yet.

## Level up: fetch statements from Gmail automatically

Instead of downloading each PDF, let the tool find statement emails in your Gmail, check they really came from the bank, and convert them.

```bash
pipx install "statement-etl-ph[gmail]"
statement-etl-ph auth client_secret.json      # once
statement-etl-ph init                          # once: a config file for your passwords
statement-etl-ph fetch --bank gcash --to moneymanager --out ~/Statements/
```

**Only mail the bank actually sent is accepted.** A `From:` address is easy to fake, so each message must carry a passing DKIM signature from the bank's own domain (`gcash.com`, `gotyme.com.ph` or `grab.com`), as verified by Gmail. Anything else is refused and left alone.

After a statement is converted, its email gets the label `statement-etl-ph/<bank>`, so the next run skips it.

### Set up your own Gmail access (about 10 minutes, free)

This tool has no shared Google login. You create your own, so nobody else's app ever gets access to your mail. Nothing is deployed, and no billing is needed.

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project (any name), and enable the **Gmail API** for it.
2. Open **Google Auth Platform** and set up the consent screen. Choose **External**, and add your own Gmail address as a test user.
3. Under **Clients**, create an OAuth client of type **Desktop app** and download its JSON file.
4. **Publish the app:** on the **Audience** page, click **Publish app** to move it from *Testing* to *In production*. Without this, Google expires your sign-in after **7 days** and fetching stops working.
5. Run `statement-etl-ph auth path/to/client_secret.json`. A browser opens. Google warns that it "hasn't verified this app": that's expected, because the app is yours. Continue, and allow access.

The token is saved to `~/.config/statement-etl-ph/token.json`, readable only by you. Revoke it any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Read-only option:** `statement-etl-ph auth client_secret.json --readonly` never changes your mailbox. Processed emails aren't labelled, so each run sees them again. For CSV output, the duplicate key stops rows being added twice; a Money Manager file is simply rewritten.

### Your bank mail goes somewhere other than Gmail?

Create a free Gmail account just for statements, and forward the bank emails to it. A dedicated account also means the token can only ever see bank mail.

- **Forward with a rule that keeps the original email intact:** a Gmail filter's forwarding, or Outlook's **Redirect to**. "Forward"-style rules and forwarding by hand change the sender, add "FW:" and can wrap the PDF inside another message; those won't match and are rejected.
- **In the receiving Gmail account,** add a filter for the bank senders set to **Never send it to Spam**. The tool doesn't search Spam.
- **Known limits:**
  - Microsoft 365 work accounts often block forwarding outside the company. Where it does forward, Safe Links and "External sender" banners break the bank's DKIM signature, so those messages are rejected.
  - Some providers, such as Yahoo and Proton, may need a paid plan for automatic forwarding.
  - Forwarding adds a few seconds to a minute of delay.

## Run it on a schedule

[docs/run-locally.md](docs/run-locally.md) has ready-to-edit schedules for macOS (`launchd`), Linux (`cron` or a systemd timer) and Windows (Task Scheduler), so `fetch` runs hourly or daily on its own.

## Use it as a library

```python
from statement_etl_ph.banks import get_bank

statement = get_bank("gcash").parser("statement.pdf", "password")
result = statement.reconcile()
assert result.ok, result.errors
for txn in statement.transactions:
    print(txn.value_date, txn.signed_amount, txn.merchant, txn.reference_no)
```

Amounts are `Decimal`, never floats.

## Never share a real statement

When reporting a problem, **never attach a real statement, a screenshot of one, or a real reference number**, and never post your PDF password. The [issue template](.github/ISSUE_TEMPLATE/parse-bug.yml) explains how to describe a layout problem using `-v` output and invented values instead.

## License

MIT. See [LICENSE](LICENSE).
