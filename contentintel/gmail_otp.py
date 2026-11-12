"""Read a login code that a platform emailed to a Gmail address, with an app password (the way E-O-A and R_C_E reach Gmail). Used ONLY for a platform whose login really emails its code: nothing
here reads SMS or an authenticator app, and nothing here is used by default (automatic re-login is not enabled for any platform; see sessions.refresh_session).

One attempt to log in, a bounded number of looks at the inbox, and the address and app password never appear in a message, a log or a result.
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from email.utils import parsedate_to_datetime

from . import contract as K

IMAP_HOST = "imap.gmail.com"


def _text_of(message):
    parts = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def fetch_code(address, app_password, *, sender_hint="", pattern=r"(?<!\d)(\d{6})(?!\d)", since_epoch=0, polls=6, wait_seconds=5, imap_factory=None, sleep=time.sleep):
    """The newest code (the first match of `pattern`) in a message from `sender_hint` received after `since_epoch`, or None after `polls` looks. Raises K.Failure(NEED_SETUP) when the mail login fails."""
    if not address or not app_password:
        raise K.Failure(K.NEED_SETUP, "GMAIL_ADDRESS and GMAIL_APP_PASSWORD are needed to read an emailed login code")
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(IMAP_HOST))
    try:
        box = factory()
        box.login(address, app_password)
    except Exception:                                               # noqa: BLE001 - the reason can name the account: say only what to check
        raise K.Failure(K.NEED_SETUP, "the Gmail login failed: check GMAIL_ADDRESS, and that GMAIL_APP_PASSWORD is a 16-character app password (it needs 2-Step Verification on that Gmail account)") from None
    try:
        for look in range(max(1, polls)):
            box.select("INBOX")
            _, found = box.search(None, "ALL")
            for number in reversed((found[0] or b"").split()[-30:]):             # newest first, the last thirty only
                _, data = box.fetch(number, "(RFC822)")
                message = email.message_from_bytes(data[0][1])
                try:
                    sent = parsedate_to_datetime(message.get("Date")).timestamp()
                except (TypeError, ValueError):
                    sent = 0
                if sent < since_epoch:
                    continue
                if sender_hint and sender_hint.lower() not in str(message.get("From", "")).lower():
                    continue
                match = re.search(pattern, _text_of(message))
                if match:
                    return match.group(1)
            if look < polls - 1:
                sleep(wait_seconds)
        return None
    finally:
        try:
            box.logout()
        except Exception:                                           # noqa: BLE001
            pass
