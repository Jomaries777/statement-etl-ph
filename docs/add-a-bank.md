# Adding a bank

A bank is one module in `src/statement_etl_ph/banks/`: a parser and a `BANK` definition. You never need, and must never share, a real statement to add one.

## 1. Learn the layout without sharing it

On your own machine, run an existing parser over your statement with `-v` to see how the PDF is laid out:

```bash
statement-etl-ph your-statement.pdf --bank gcash --dry-run -v
```

It fails for a new bank, but the debug lines show what pdfplumber sees. For a closer look, print the words and their positions yourself:

```python
import pdfplumber

with pdfplumber.open("your-statement.pdf", password="...") as pdf:
    for word in pdf.pages[0].extract_words():
        print(round(word["x0"]), round(word["x1"]), round(word["top"]), word["text"])
```

Note the **positions** of the column headers and of a few amounts, never the values themselves. Those positions are all the rest of the work needs.

## 2. Write the parser

Most statements are a borderless table, and `pdf_geometry` already does the hard part:

- `cluster_rows(words, tolerance, sort_key)` groups words into visual rows.
- `find_money_columns(rows, headers)` finds where the money columns sit from their header row.
- `assign_money_columns(row, columns, tolerance)` assigns each amount on a row to the nearest column by its right edge.

What differs per bank is the vocabulary: the header names, how the period is printed, what the totals rows are called, and how forgiving the tolerances need to be. Pass yours in; don't change another bank's.

The parser has one shape, `parse_statement(path, password) -> Statement`. Fill in the printed totals (`starting_balance`, `ending_balance`, `total_debit`, `total_credit`) whenever the statement prints them: they're what `reconcile()` checks the rows against.

**Reference numbers.** If the bank prints one per row, use it. It becomes the duplicate key. If not, build a stable one from the date, description, amounts and running balance, as `banks/gotyme.py` does, so two identical payments on one day stay two rows.

## 3. Declare the bank

At the bottom of your module:

```python
BANK = BankDefinition(
    name="examplebank",
    display_name="Example Bank",
    parser=parse_statement,
    gmail_query='from:statements@examplebank.com.ph subject:"Statement" has:attachment',
    dkim_domains=("examplebank.com.ph",),
    password_env="EXAMPLEBANK_PDF_PASSWORD",
)
```

Then add it to the tuple in `banks/__init__.py`.

**`dkim_domains`:** in Gmail, open one of the bank's emails, choose **Show original**, and read the `Authentication-Results` line. Use the domain on the `dkim=pass` result that belongs to the bank (`header.i=@…` or `header.d=…`). Never add an email provider's domain such as `amazonses.com` or `sendgrid.net`: they sign mail for anyone who pays them.

## 4. Build a synthetic statement

Copy `tests/fixtures/make_fixture.py` into `tests/fixtures/make_<bank>_fixture.py` and change it to draw your bank's layout:

- the same header words, at the same x positions you measured
- **invented** rows: made-up merchants, made-up reference numbers, round amounts
- a running balance and printed totals that genuinely add up
- encrypted with a made-up password, using pikepdf

Cover the cases that break parsers: a credit row, a thousands separator, a long description, a row with an empty description, and a second page if real statements have one.

## 5. Test it

In `tests/test_parse_<bank>.py`, at least:

- every row is extracted, and every reference and description matches the generator's rows exactly
- `reconcile().ok` is true
- a wrong password is rejected
- a row with an empty description keeps every later amount on its own row

## 6. Before you open the pull request

- [ ] No real names, account or mobile numbers, card digits, reference numbers or addresses anywhere: code, tests, comments or commit messages.
- [ ] No real PDFs, screenshots or `Authentication-Results` headers. Invent values with the same shape.
- [ ] `pytest` passes.
