# email_fetcher.py — deep-dive

194 lines. Polls Gmail via IMAP for Greenhouse security-code emails. **Read this when changing the IMAP poll, the search heuristics, or the code-extraction regex.**

> **Self-update reminder:** edit this doc whenever you change search terms, parsing, or the polling contract.

## Public surface

### `fetch_security_code(email_addr, app_password, started_at=None, timeout_seconds=90, poll_interval=2.0) -> str | None`

Polls Gmail until it finds a fresh Greenhouse security-code email or times out. Returns the code string or `None`.

- `started_at` defaults to `now - 30s`. Older messages are ignored — prevents picking up a stale code email from earlier in the day.
- Polls every `poll_interval` seconds (default 2.0).
- On `imaplib.IMAP4.error` (auth/protocol problems): bails immediately with `None` — no retry, since these don't recover.
- On other exceptions: stores the first error message and keeps polling.
- Logs each attempt + final outcome via Rich console.

Used by:
- `runner._wait_for_security_code` (90s default to handle Greenhouse's email delay).
- `cli.check-gmail` (15s, 7-day lookback for connection smoke-test).

## Constants

```python
GREENHOUSE_FROM_HINT = "greenhouse"   # substring match in From header
SUBJECT_HINT = "security code"        # not currently referenced (legacy)
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
```

The `SUBJECT_HINT` constant is dead code — the search-term list inside `_try_fetch` is the actual source of truth. Could be deleted.

## Flow inside `_try_fetch`

1. **Login** via `imaplib.IMAP4_SSL`.
2. **Select mailbox.** Calls `_find_all_mail` to locate Gmail's "All Mail" folder (locale-independent — uses the `\\All` IMAP flag rather than the localized name). Falls back to `INBOX`. Selecting "All Mail" catches messages routed to Promotions/Updates that bypass the inbox.
3. **Search broadens progressively** until results are non-empty:
   - `(SUBJECT "security code" SINCE "<dd-Mon-yyyy>")`
   - `(SUBJECT "verification code" SINCE ...)`
   - `(SUBJECT "your code" SINCE ...)`
   - `(FROM "greenhouse" SINCE ...)`
4. **Iterate matches in reverse** (newest first). For each:
   - Fetch RFC822, parse with `email.message_from_bytes`.
   - Skip if older than `started_at` (`_is_recent` parses the `Date:` header).
   - Skip if `From:` doesn't contain `"greenhouse"` (case-insensitive).
   - Extract body via `_get_body` (prefer `text/plain`; fall back to `text/html` with crude tag stripping).
   - Run `_extract_code(body)` → return on first match.
5. **Always logout** in `finally`.

## Code extraction (`_extract_code`)

Three strategies, tried in order:

1. **Anchored regex 1:** `r"code\s+into\s+the\s+security\s+code\s+field[^:]*:\s*([A-Za-z0-9]{6,12})"` — matches Greenhouse's literal "Copy and paste this code into the security code field on your application:" boilerplate.
2. **Anchored regex 2:** `r"security code[^:]*:\s*([A-Za-z0-9]{6,12})"` — looser fallback.
3. **Line-by-line:** any standalone 6–12 char alphanumeric token that has at least one letter AND (a digit OR mixed case). Skips bare year-like tokens (`"2026"`) and common single-case words.

Greenhouse codes can be all-letter mixed case (e.g. `"frvCRokn"`) — that's why the fallback requires either digits or mixed-case letters, not just digits.

## Helpers

### `_find_all_mail(M) -> str | None`
Calls `M.list()`, scans for a mailbox line containing `\\All`, extracts the quoted name from the trailing `"..."`. Returns `None` if not found.

### `_is_recent(msg, started_at) -> bool`
Parses `msg["Date"]` via `parsedate_to_datetime`. Returns `True` if the parsed datetime is `>= started_at`. **Returns `True` on parse failure or missing header** (better to consider it recent than skip a real code).

### `_get_body(msg) -> str`
- Multipart: prefer `text/plain` parts via `msg.walk()`. Fall back to `text/html` and strip tags with `re.sub(r"<[^>]+>", " ", html)` — crude but enough for the code extraction regexes.
- Non-multipart: just `_decode_part(msg)`.

### `_decode_part(part) -> str`
`get_payload(decode=True)`, decode with the part's content-charset (UTF-8 fallback), `errors="ignore"`.

## Common edits

- **Add a search term:** insert into the `for search_term in [...]` list in `_try_fetch`. Order matters — narrower first to avoid false positives.
- **Tighten the From check:** `GREENHOUSE_FROM_HINT` is a substring; replace with a list of known sender domains if false positives appear.
- **Loosen the code regex:** if Greenhouse changes format, adjust the two anchored regexes in `_extract_code` first; treat the line-by-line fallback as last resort.
- **Support a non-Gmail provider:** replace `IMAP_HOST` and the All-Mail logic; selecting `INBOX` works for most providers but loses Promotions visibility.

## Gotchas

- **Gmail App Passwords required.** Regular Gmail passwords won't work for IMAP — users must enable 2FA and create an app password. `cli.check-gmail` is the smoke test.
- **`SUBJECT_HINT` is unused** (legacy constant). Don't trust comments that reference it.
- **HTML stripping is naïve.** A `<style>` block could inject arbitrary text; the line-by-line fallback could pick up a CSS class name that happens to match the regex. Hasn't been a real problem so far.
- **The first IMAP error short-circuits** but other errors loop until timeout. If you want all errors to bail, change the `except` block in `fetch_security_code`.
- **`time.sleep(poll_interval)` blocks the whole thread.** This is fine for the CLI but would wedge an async context — don't call this from inside a Playwright async loop without offloading.
