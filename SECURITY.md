# Security design

Single user, single machine at a time. The goal: your financial data is unreadable to
anyone who gets the file (cloud sync, lost laptop, USB stick) and the app can't be
driven by other websites or programs while it's running.

## Data at rest

- The whole SQLite database is encrypted into one file, `budget.vault`.
- Encryption: **AES-256-GCM** with a random 256-bit data key and a fresh random 96-bit
  nonce on every save.
- The data key is kept in the file header, wrapped (AES-256-GCM) under a key derived with
  **Argon2id** (64 MiB, 3 passes, 4 lanes, random 16-byte salt) from the passphrase, and
  optionally a second copy wrapped the same way under the recovery code.
- The whole header (format version, key slots, KDF parameters, nonce) is authenticated, so
  tampering with any byte of the file is detected. Older format-1 files, whose key came
  straight from the passphrase, still open and get a fresh random data key on the next save.
- Plaintext never touches disk: the database is decrypted straight into memory
  (`sqlite3.deserialize`) and re-encrypted on every committed change.
- Saves are atomic (temp file, fsync, rename), so a crash or power loss can't leave a
  half-written vault. Each unlock also keeps an encrypted backup copy (last 30).
- Uploaded **documents** are the one thing kept outside the vault, because the vault is
  rewritten in full on every save and a library of PDFs would make every click slow. Each
  file gets its own random AES-256-GCM key and a random name in `data/documents/`; the key
  lives inside the encrypted database, so a file on its own (or in cloud sync) is
  unreadable, and the folder listing gives away nothing. The file name is used as the
  authenticated additional data, so files can't be swapped between records. Deleting a
  document or a version deletes its file.
- Wrong passphrase and corrupted file are both reported. The only way in without the
  passphrase is the optional recovery code (below).

## Passphrase required

Every vault needs a passphrase, and the app asks for it each time it opens (and after 15
idle minutes). A vault created during the app's early no-passphrase mode opens to a **Set a
passphrase** page first: the file is re-encrypted with the new passphrase before anything
else loads, and no extra unprotected backup is written. Backups in `data/backups` made
*before* that were saved without a passphrase, and anyone holding them can read them, so
delete them once the passphrase is set.

The one exception is dev mode (`--dev`, `Dev.cmd`), for working on the app itself: it runs on a
separate `data-dev/` vault, on its own port, that opens without a passphrase. It never touches
`data/`, and it isn't the default; don't keep real data in it.

## Recovery code

Settings can create a **recovery code**: 160 random bits, shown once as 32 characters in
groups of four. It opens the vault if the passphrase is lost, and the app then makes you
choose a new passphrase before anything else loads. Creating, replacing or removing the
code asks for the current passphrase. Only the data key wrapped under the code is stored,
so the app can't show the code again, and the code never goes into the session cookie, a
flash message or a log. Changing the passphrase keeps the code working; making a new code
cancels the old one.

The code is a second key to everything, so keep it offline (printed, somewhere safe): not
in OneDrive, and not in the same password manager as the passphrase. Without the passphrase
or the code the vault can't be opened, and backups made before the code existed open only
with the passphrase they were made with.

## Imported files

- CSV and OFX/QFX files are parsed in memory and only the transactions reach the vault.
- OFX is read by plain pattern matching over the text, never an XML parser, so an OFX 2 file
  can't expand entities or make the app fetch anything.
- Error messages about a file never quote its contents (they can end up in the session
  cookie); an unexpected currency code is named only if it looks like one.

## While running

- The server binds to `127.0.0.1` only; it is never reachable from the network.
- **Host header allow-list** (`127.0.0.1:<port>`, `localhost:<port>`) defeats DNS rebinding.
- **CSRF token** on every POST, plus an **Origin check** and `SameSite=Strict` cookies.
- The session cookie is bound to the current unlock. Another browser or local program
  hitting the port without that cookie gets the unlock screen, not your data.
- **Auto-lock** after 15 idle minutes (`--idle-minutes`) closes the database and drops the key.
- Strict **Content-Security-Policy** (self only, no inline script); no CDN or
  third-party assets, so the pages themselves load nothing from the internet.
- `Cache-Control: no-store` keeps financial pages out of the browser's disk cache. The one
  exception is a personal background video in `static/` (`.mp4`/`.webm`, no financial data),
  which the browser may keep privately for a week.

## Network access (price refresh only)

The app makes outbound requests in exactly one case: you click **Refresh prices** on
the Brokerage page. Then:

- Only **ticker symbols / coin ids** are sent. Shares, amounts, account names and
  anything else about you stay local.
- Requests go over **HTTPS** to a fixed allow-list: `query1.finance.yahoo.com`,
  `api.coingecko.com`, `finnhub.io`, `www.alphavantage.co`. Redirects to any other host
  are refused. Responses are size-capped and parsed strictly.
- Optional **API keys** are stored inside the encrypted vault (never in config files or
  environment variables), shown masked in the UI, and sent only to their own provider.
  Finnhub and CoinGecko keys travel in request headers; Alpha Vantage only accepts its
  key as a URL parameter (still protected by HTTPS).
- The providers can see your IP address and which tickers you hold. Use manual prices
  if that matters for a particular holding.

## Known limits

- Python can't reliably wipe memory, so while unlocked the key and data sit in process
  memory. Malware running as your user can read it. Lock the app when you're not using it.
- The passphrase, and the recovery code if you made one, are the only secrets. Use a long one (4 to 5 random words) from a
  password manager.
- A copy of the vault in OneDrive or on a USB stick is only as strong as the passphrase.
  Anyone holding the file can try guesses offline; Argon2id makes each guess expensive.

## Repository hygiene

Documents are served back with `Content-Disposition` and a type from a small allow-list;
anything else is handed over as `application/octet-stream` attachment, and `nosniff` is set
on every response, so an uploaded HTML or SVG file can never run as a page in the app's origin.

`.gitignore` excludes `data/`, `private/`, `*.vault` and common export formats
(csv, xlsx, ofx, pdf). Put raw statements in `private/`.
Never commit financial data, even encrypted.
