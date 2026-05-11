"""Fetch the Greenhouse security code from a Gmail inbox via IMAP."""
from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from rich.console import Console

# Greenhouse has used several sender domains over the years. We filter on the substring
# 'greenhouse' in the From header rather than a single fixed address.
GREENHOUSE_FROM_HINT = "greenhouse"
SUBJECT_HINT = "security code"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

_console = Console()


def fetch_security_code(
    email_addr: str,
    app_password: str,
    started_at: datetime | None = None,
    timeout_seconds: int = 90,
    poll_interval: float = 2.0,
) -> str | None:
    """Poll Gmail for a fresh Greenhouse security-code email. Returns the code or None on timeout.

    Only matches emails received at/after `started_at` (defaults to ~30s before call time)
    so a stale code email from earlier in the day isn't picked up.
    """
    started_at = started_at or (datetime.now(timezone.utc) - timedelta(seconds=30))
    deadline = time.time() + timeout_seconds
    first_error: str | None = None
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        _console.print(f"[dim]Gmail poll attempt {attempts}...[/dim]")
        try:
            code = _try_fetch(email_addr, app_password, started_at)
            if code:
                return code
        except imaplib.IMAP4.error as e:
            # Auth / protocol errors won't recover by retrying — surface immediately.
            _console.print(f"[red]Gmail IMAP error: {e}[/red]")
            return None
        except Exception as e:
            if first_error is None:
                first_error = f"{type(e).__name__}: {e}"
            _console.print(f"[dim]Gmail poll error: {type(e).__name__}: {e}[/dim]")
        time.sleep(poll_interval)
    if first_error:
        _console.print(f"[yellow]Gmail poll: every attempt failed. First error: {first_error}[/yellow]")
    else:
        _console.print(f"[yellow]Gmail poll: no matching email after {attempts} attempts.[/yellow]")
    return None


def _try_fetch(email_addr: str, app_password: str, started_at: datetime) -> str | None:
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        M.login(email_addr, app_password)
        # Search "All Mail" so we catch messages routed to Promotions/Updates/labels
        # that bypass the inbox.
        folder = _find_all_mail(M) or "INBOX"
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            M.select("INBOX", readonly=True)
        since_str = started_at.strftime("%d-%b-%Y")
        # Try progressively broader searches so we catch Greenhouse regardless of exact subject.
        ids: list[bytes] = []
        for search_term in [
            f'(SUBJECT "security code" SINCE "{since_str}")',
            f'(SUBJECT "verification code" SINCE "{since_str}")',
            f'(SUBJECT "your code" SINCE "{since_str}")',
            f'(FROM "greenhouse" SINCE "{since_str}")',
        ]:
            typ, data = M.search(None, search_term)
            if typ == "OK" and data and data[0]:
                ids = data[0].split()
                _console.print(f"[dim]IMAP search '{search_term}' → {len(ids)} message(s)[/dim]")
                break
        if not ids:
            return None
        for msg_id in reversed(ids):
            typ, msg_data = M.fetch(msg_id, "(RFC822)")
            if typ != "OK":
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            if not _is_recent(msg, started_at):
                continue
            sender = (msg.get("From") or "").lower()
            if GREENHOUSE_FROM_HINT not in sender:
                continue
            body = _get_body(msg)
            code = _extract_code(body)
            if code:
                return code
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return None


def _find_all_mail(M: imaplib.IMAP4_SSL) -> str | None:
    """Return the Gmail 'All Mail' folder name (locale-independent via the \\All flag)."""
    typ, mailboxes = M.list()
    if typ != "OK" or not mailboxes:
        return None
    for mb in mailboxes:
        if not mb:
            continue
        line = mb.decode("utf-8", errors="ignore") if isinstance(mb, bytes) else str(mb)
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
    return None


def _is_recent(msg: email.message.Message, started_at: datetime) -> bool:
    date_hdr = msg.get("Date")
    if not date_hdr:
        return True
    try:
        msg_dt = parsedate_to_datetime(date_hdr)
        if msg_dt is None:
            return True
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        return msg_dt >= started_at
    except Exception:
        return True


def _get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        # Prefer text/plain
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode_part(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = _decode_part(part)
                # Strip tags crudely so the code-extraction regexes work on either form.
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    return _decode_part(msg)


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="ignore")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="ignore")


_CODE_TOKEN = re.compile(r"\b([A-Za-z0-9]{6,12})\b")


_CODE_DENY_WORDS = {
    "greenhouse",
    "mygreenhouse",
    "linkedin",
    "indeed",
    "anthropic",
    "password",
    "security",
}


def _extract_code(body: str) -> str | None:
    """Greenhouse formats:

    Application security code email:
        ``Copy and paste this code into the security code field on your application:\\n\\nCODE``
    MyGreenhouse login email:
        ``Your security code is:\\n\\n********\\nCODE\\n********``
    """
    # Application-form security code.
    m = re.search(
        r"code\s+into\s+the\s+security\s+code\s+field[^:]*:\s*([A-Za-z0-9]{6,12})",
        body, re.I | re.S,
    )
    if m:
        return m.group(1)
    # MyGreenhouse sign-in email — code wrapped in `********` separators.
    m = re.search(
        r"security code is:[^A-Za-z0-9]+([A-Za-z0-9]{6,12})",
        body, re.I | re.S,
    )
    if m:
        return m.group(1)
    # Generic "security code: CODE" form.
    m = re.search(r"security code[^:]*:\s*([A-Za-z0-9]{6,12})", body, re.I | re.S)
    if m:
        return m.group(1)

    # Fallback: any line that's a single 6–12 char alphanumeric token that contains
    # at least one letter (avoids bare years like "2026") and is mixed-case or has digits
    # (avoids common English words). Greenhouse codes can be all-letter (e.g. "frvCRokn").
    for line in body.splitlines():
        s = line.strip()
        if not _CODE_TOKEN.fullmatch(s):
            continue
        if s.lower() in _CODE_DENY_WORDS:
            continue
        has_letter = any(c.isalpha() for c in s)
        has_digit = any(c.isdigit() for c in s)
        has_mixed_case = any(c.isupper() for c in s) and any(c.islower() for c in s)
        if has_letter and (has_digit or has_mixed_case):
            return s
    return None
