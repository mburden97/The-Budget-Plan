# The Budget Plan — notes for Claude (and other contributors)

Personal, local-only, encrypted budget app (Python 3.12+, Flask + waitress, SQLite in memory,
vault file = Argon2id + AES-256-GCM). See README.md for layout and SECURITY.md for the threat model.

## Hard rules
- Never commit, print, log, or paste financial data. `data/`, `private/`, `*.vault` and export
  formats are git-ignored; keep it that way. Raw statements belong in `private/`.
- No CDNs, analytics, or third-party assets; all JS/CSS is served from `web/static/`. The only
  network access is the user-initiated price and dividend refresh in `quotes.py` (HTTPS
  allow-list, symbols only). Don't add other outbound calls, and never send amounts or account
  details.
- API keys live in the vault's `settings` table. Never log them or render them unmasked.
- The Flask session cookie is signed, not encrypted: never put financial data in `session` or in
  flash messages (e.g. don't echo CSV cell contents in errors).
- Imported CSV and OFX files are parsed in memory; transient state goes in `store.scratch`
  (cleared on lock), never on disk. The one exception is `documents.py`: uploaded documents are
  encrypted individually (random key per file, kept in the vault; random file name; the name is
  the AEAD's additional data) and written to `data/documents/`. They stay out of the vault
  because it is rewritten in full on every save. Never write an upload anywhere else, and never
  render a `key_hex`.
- Don't weaken the web guards in `web/__init__.py` (host allow-list, CSRF, origin check, CSP).
- Every vault needs a passphrase: `ALLOW_BLANK_PASSPHRASE = False`. Don't turn it back on. A vault
  made in the old no-passphrase mode opens to `/set-passphrase`. Never type, ask for, store or
  otherwise handle a user's passphrase. The one exception is dev mode (`--dev` / `Dev.cmd`): a
  separate `data-dev/` vault on port 8767 that opens without one, via `blank_allowed()`. Keep it
  opt-in, keep it off `data/`, and don't make it the default.
- Vault format 2 (`vault.py`): a random data key encrypts the database; the header holds it
  wrapped once per secret (passphrase slot, optional recovery-code slot). Format-1 files still
  open and are re-keyed. A recovery code is shown once, in the response that creates it: never
  put it in the session, a flash message, a redirect, a log or a test fixture.
- Money is integer cents. Never floats. Rates/shares/prices are Decimal stored as TEXT.
- Schema changes: add `src/budgetapp/migrations/NNN_name.sql`; never edit an applied migration.
- Domain logic lives in plain modules (`planning.py`, `loans.py`, `networth.py`) taking a
  `sqlite3.Connection`; web views stay thin and write through `store.write()`.
- Per-budget-line totals (Actual, Trends, Subscriptions) must read the `allocations` view, which
  is each transaction's remainder plus its `transaction_splits`. Cash / per-account totals read
  `transactions`. A rule may carry a split (`split_line_item_id`, `split_amount_cents`).
- Tests use invented labels and amounts only. Never put anyone's real line names, payees,
  balances, rates or dates into tests, commit messages or docs.

## Working with a live vault
- A running app holds the decrypted database in memory and rewrites the vault on every save, so
  change data only through the running app (its pages, or HTTP like the browser), never by
  editing the vault file.
- The app caches templates and code: restart it after a code change.
- Categorizing: `POST /transactions/<id>/categorize` (`category` = line id, `x` = not budgeted;
  `remember`+`pattern` saves a rule). Rules apply only to uncategorized transactions.
  Precedence on import: description rules (longest match) → bank-category map → uncategorized.
- Bank CSV quirks are handled in `imports.py` (see README, Importing); AmEx detailed exports put
  the category in column 11 (index 10).
- OFX/QFX: `ofx.py` turns a statement into a `ParsedCsv` with fixed columns and `ofx.MAPPING`
  (a FITID `id_col`). Import hashes built from a bank id start with `ref:`; `import_rows`
  also matches a row against rows imported the *other* way (same day and amount, each used
  once), so an account switching between CSV and OFX isn't imported twice. OFX imports don't
  save a column profile. Never parse OFX with an XML parser.
- Dates: `date_order()` decides month- or day-first once per file; `build_rows` takes
  `day_first` so a preview sample uses the whole file's answer.
- Loans: `plan_payoff` spends one fixed monthly budget (every loan's planned payment plus any
  extra): minimums first, then each promo loan up to the pace that clears it by its deadline,
  then the strategy's order. A minimum is fixed, a percent of the balance, or none.

## Commits
- Run lint and tests and check pytest's exit code before committing (in PowerShell, piping
  through Select-Object hides it).
- In PowerShell, `git commit -m @'...'@` breaks if the message contains double quotes (git then
  reports `pathspec ... did not match any file(s)` and commits only the subject line). For any
  message with quotes, use a Bash heredoc instead: `git commit -F - <<'MSG' ... MSG`.

## Commands (on Windows, `Budget.cmd` keeps the venv in `%LOCALAPPDATA%\TheBudgetPlan\venv`)
- Tests: `python -m pytest`
- Lint:  `python -m ruff check src tests`
- Run:   `.\Budget.cmd` (or `python -m budgetapp --no-browser`)
