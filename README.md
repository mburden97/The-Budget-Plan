# The Budget Plan

A personal, local-only budget app. Everything lives in **one encrypted file** that
you can back up or copy anywhere. No accounts, no cloud service, no telemetry. The
only network access is an optional price refresh for investments (see
[SECURITY.md](SECURITY.md)).

## Features

| Page | What it does |
|---|---|
| **Home** | Net worth trend, money in/out, left to assign, month spending ring, category bars, debt-free date, emergency fund, investments, sinking funds, and nudges (stale balances, uncategorized transactions, funds falling behind). **Log** a loan payment (lowers the loan balance) or a transfer to savings (raises the savings balance, lowers the source account; optionally for a sinking fund, which updates its Saved) right from the home page. **Next 14 days**: repeating bills and paychecks projected against the checking balance, with a warning if it could go below $0. The emergency fund is the flagged savings balance minus what sinking funds have saved. Optional background video: put `home-bg.mp4` (or `.webm`) in `web/static/` (git-ignored); it loops muted under a 65% tint |
| **Documents** | Every financial document in one place: upload a lease, a policy, a statement or a pay stub, then drop a newer version on top whenever it changes. Each document keeps its history, shows **Last updated**, and can be viewed or downloaded. Files are encrypted one by one (see [SECURITY.md](SECURITY.md)) and kept in `data/documents/`, not in the vault, so saving stays fast. Nothing here feeds the rest of the app, except that a CSV can be handed straight to the Transactions import |
| **Budget → Plan** | Categories (income / expense / savings / debt, add your own) and editable line items at any frequency. A new vault can start with a dozen common lines at $0, to rename, fill in or delete. Months are counted as **two paychecks** by default (bi-weekly ×2, weekly ×4; 3rd-paycheck months shown as yearly windfalls) or averaged over the year. Left to assign, savings rate, essential flags |
| **Budget → Actual** | Planned vs. actual per line item for any month |
| **Budget → Subscriptions** | Itemizes the charges in any line named for subscriptions/memberships: cadence, usual charge, cost per month, last/next charge, card, 12-month spend. "Not a subscription" moves a merchant out; "Missing one?" and "Possible subscriptions" move one in. **Cancelled** takes one off the active list and flags any charge that posts after the cancel date (on the tab, on the home page and on import) |
| **Transactions** | Bank/card CSV, OFX and QFX import (see *Importing* below) with column mapping remembered per account, duplicate-safe re-imports, "always categorize like this" rules, bank-category matching, transfers excluded from the budget, manual entry. Pick **+ New budget line…** in a row to make a line (or a new category) for that transaction on the spot. **Split** a charge across budget lines (e.g. part of a phone bill is really a cloud-storage plan), optionally with a rule that splits every matching charge |
| **Trends** | Complete-month spending over 6/12/24 months by category or budget line: stacked bars plus a table with sparklines, averages and 3-month change. **Projected net worth**: today's balances rolled forward 1-10 years at the current plan (savings lines, anything left to assign, loan schedules, APY; optional 3rd paychecks, freed loan payments and an investment growth rate you choose) |
| **Loans** | Balance, APR, promo end date (warns on the home page when the planned payment won't clear the balance before a promo ends), payoff date, schedule, "what if I pay $X extra", avalanche vs. snowball from one fixed monthly budget, payment log that updates the balance. A minimum can be a fixed amount, a percent of the balance, or none (a personal loan). The payoff comparison funds any promo loan at the pace that clears it before its deadline first, whatever its APR, then follows the strategy, and flags a plan that would leave a promo balance. Paying a loan off to $0 (payment, home-page log or balance check-in) rains money; clearing the last one rains harder |
| **Brokerage** | Stocks, ETFs, funds and crypto; one-click price refresh from free APIs; value, cost basis, gain; allocation vs. target. Per-holding **dividend yield**, reinvested (DRIP) or taken as cash, which the Trends projection compounds; **Refresh dividend yields** looks up what each holding paid over the last year (symbols only, same providers as prices) |
| **Net worth** | All accounts, balance check-in, history chart, stale-balance reminder, APY on savings accounts (estimated monthly interest), archive / show archived |
| **Goals** | Emergency fund (months of essential expenses covered, interest earned) and sinking funds for irregular bills |
| **Settings** | Base income (amount and frequency of each income line, with the effect on Left to assign), how a month is counted, reminders, emergency-fund target, optional price API keys, passphrase, recovery code (opens the vault if the passphrase is lost), the cat (a little black cat that walks along the top bar, sits, naps, and stops when clicked) |

## Run it

Requires Python 3.12+ (`py --version`).

**Windows:** double-click `Budget.cmd`. It keeps a Python environment in
`%LOCALAPPDATA%\TheBudgetPlan\venv` (outside OneDrive), reinstalls packages whenever
`pyproject.toml` changes, and opens `http://127.0.0.1:8766`.

**Manually / other OS:**

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
.venv/bin/python -m budgetapp          # options: --port, --data-dir, --idle-minutes, --no-browser
```

A passphrase is required. Keep it in a password manager, and create a **recovery code** in
Settings (print it and keep it offline): without one or the other the vault can't be opened
(see SECURITY.md).

## Where your data lives

```
data/
  budget.vault        <- everything, encrypted. Back up / carry this one file.
  backups/            <- encrypted copy taken on each unlock (last 30 kept)
  documents/          <- uploaded documents, each encrypted under its own key. Back this up too
private/              <- raw spreadsheets, statements, PDFs to import (never committed)
```

`data/` and `private/` are git-ignored. Override the data location with `--data-dir`
or `BUDGET_DATA_DIR`. Delete raw files from `private/` once imported: they are not
encrypted.

## Importing

| Source | How to export | Notes |
|---|---|---|
| Chase checking | chase.com → account → Download activity → CSV | Rows end with an extra comma (handled). No bank category |
| Chase credit card | same | Has a Category column → pick it as *Bank category* |
| American Express | Statements & Activity → Download → CSV, include all details | Purchases are positive → *Flip signs* (pre-ticked for card accounts). Detailed export has a Category column |
| Bank of America | Download transactions → CSV | Balance summary above the table and $0 fee-waiver rows (both skipped) |
| Any bank offering OFX / QFX | Download → *Quicken*, *Money*, *OFX*, *QFX* or *Web Connect* | The best choice when offered: standard columns (nothing to map) and the bank's own id on every transaction, so re-imports are exact. Switching an account from CSV to OFX doesn't import its history twice. One account per file, US dollars only |

Any other bank's CSV works too: columns are guessed from the header words and, when those
don't say (or there is no header row at all), from what the cells hold. Dates are read one
way round for the whole file, day-first only when some day is over 12 (the preview says so,
and says when every date could go either way). Amounts marked `CR` count as money in, `DR`
or a trailing minus as money out. A unique **Transaction ID** column can be picked to make
re-imports exact, as with OFX.

After importing: **Transactions → Bank categories** matches bank labels to budget lines
once; **rules** (description contains X) win over bank categories.

## Development

**Dev mode:** double-click `Dev.cmd` (or run `python -m budgetapp --dev`) to work on the app
without a passphrase. It uses a separate `data-dev/` vault on port 8767, created with the
starter budget lines on first run, and opens straight to the dashboard. That vault is not
protected, so use made-up data only; your real vault in `data/` is untouched.

```bash
python -m pytest          # tests
python -m ruff check .    # lint
```

```
src/budgetapp/
  vault.py         encrypted file format (seal / unseal / atomic write)
  store.py         unlocked session: in-memory SQLite + key, save-on-commit, backups, auto-lock
  db.py            serialize/deserialize + migrations (migrations/NNN_*.sql, PRAGMA user_version)
  money.py dates.py charts.py      cents/percent parsing, calendar helpers, server-side SVG
  planning.py      budget categories, line items, two-paycheck / average month
  transactions.py  transactions, rules, bank-category mapping, budget vs. actual
  imports.py       bank CSV parsing (header detection, column mapping, de-duplication)
  ofx.py           OFX / QFX statements into the same pipeline (pattern matching, no XML parser)
  subscriptions.py itemize subscriptions, cadence, candidates, release
  trends.py        monthly spending by category / line
  loans.py         loan math, payoff strategies, loan storage
  brokerage.py     holdings, valuation, allocation
  quotes.py        price providers (the only network code)
  networth.py      accounts, balance snapshots, APY interest, net worth history
  goals.py         sinking funds, emergency fund
  documents.py     uploaded documents: per-file encryption, versions
  settings.py      key/value settings (inside the vault)
  web/             Flask views + templates, served on 127.0.0.1 only
```

Conventions: money is always integer cents; rates, share counts and prices are
`Decimal` stored as TEXT; dates are ISO `YYYY-MM-DD`. Schema changes go in a new
numbered migration file, never by editing an applied one.

## License

[GPL-3.0](LICENSE). This is personal software shared as it is: no warranty, and nothing it
shows is financial advice. Check its numbers against your own statements.
