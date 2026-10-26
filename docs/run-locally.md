# Run it on your own computer

Everything here runs on your machine. The only Google Cloud step is the free project that holds your Gmail OAuth client ([README](../README.md#set-up-your-own-gmail-access-about-10-minutes-free)); nothing is deployed.

## 1. Install

Python 3.10 or newer, on macOS, Linux or Windows.

```bash
pipx install "statement-etl-ph[gmail]"
```

Drop `[gmail]` if you'll only convert PDFs you download yourself.

## 2. Sign in to Gmail once

```bash
statement-etl-ph auth path/to/client_secret.json
```

A browser opens for consent. The token is saved to `~/.config/statement-etl-ph/token.json` (`%APPDATA%\statement-etl-ph\token.json` on Windows), readable only by you. Add `--readonly` if the tool must never change your mailbox.

## 3. Write your config

```bash
statement-etl-ph init
```

This writes `~/.config/statement-etl-ph/statement-etl-ph.json`, also readable only by you. Fill in:

- each bank's `password` (or leave it empty and set `GCASH_PDF_PASSWORD` / `GOTYME_PDF_PASSWORD` instead)
- `account_name`, if your Money Manager or YNAB account is named differently
- `output.to` (`moneymanager`, `csv` or `ynab`) and `output.dir`
- `gmail.since`: how far back to search (`30d`, `2w`, `yesterday`, `all`)
- for YNAB, `ynab.token` (or set `YNAB_ACCESS_TOKEN` instead) and `ynab.budget_id`. A scheduler doesn't load your shell's environment variables, so a scheduled run is simplest with the token in this file.

Remove the banks you don't use: `fetch` with no `--bank` reads every bank listed.

## 4. Run it

```bash
# A PDF you already have
statement-etl-ph statement.pdf --bank gcash --to moneymanager

# Everything new in Gmail, for every bank in your config
statement-etl-ph fetch

# See what would happen, write nothing, label nothing
statement-etl-ph fetch --dry-run

# Show how a PDF was read, column positions included
statement-etl-ph statement.pdf --bank gcash --dry-run -v
```

A statement whose rows don't match its printed totals is never written, and its email isn't labelled, so the next run tries it again.

## 5. Run it on a schedule (optional)

Each schedule runs `statement-etl-ph fetch`. Because converted emails are labelled, running often is harmless: a run with nothing new does nothing.

First find the full path of the command, since schedulers don't load your shell's `PATH`:

```bash
which statement-etl-ph
```

### macOS (launchd)

1. Copy [`deploy/local/com.statement-etl-ph.fetch.plist`](../deploy/local/com.statement-etl-ph.fetch.plist) to `~/Library/LaunchAgents/`.
2. Edit the command path and the log paths in it.
3. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.statement-etl-ph.fetch.plist
   ```

It runs every hour while you're logged in, and once on login.

### Linux (systemd timer)

1. Copy [`deploy/local/statement-etl-ph.service`](../deploy/local/statement-etl-ph.service) and [`statement-etl-ph.timer`](../deploy/local/statement-etl-ph.timer) to `~/.config/systemd/user/`.
2. Edit the command path in the `.service` file.
3. Enable it:
   ```bash
   systemctl --user enable --now statement-etl-ph.timer
   ```

Or with cron (`crontab -e`), hourly:

```
0 * * * * /home/you/.local/bin/statement-etl-ph fetch >> /home/you/statement-etl-ph.log 2>&1
```

### Windows (Task Scheduler)

In PowerShell, using the path from `where.exe statement-etl-ph`:

```powershell
schtasks /Create /SC HOURLY /TN "statement-etl-ph fetch" /TR "C:\Users\you\.local\bin\statement-etl-ph.exe fetch"
```

## What a local run can't do

An import happens when the command runs, not the moment the email arrives. Reacting instantly needs a public address for Gmail's notifications to reach, which a laptop doesn't have.
