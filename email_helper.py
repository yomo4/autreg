"""
email_helper.py — Поиск ссылки верификации Kleinanzeigen через IMAP.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import re
import time
from email.header import decode_header
from typing import Optional

from config import IMAP_SERVERS, EMAIL_CHECK_TIMEOUT


def _guess_imap(email_address: str) -> tuple[str, int]:
    domain = email_address.split("@")[-1].lower()
    if domain in IMAP_SERVERS:
        return IMAP_SERVERS[domain]
    # Generic fallback
    return (f"imap.{domain}", 993)


def _extract_text(msg: email.message.Message) -> str:
    """Вернуть текстовое / HTML тело письма."""
    body_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode(charset, errors="ignore"))
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode(charset, errors="ignore"))
    return "\n".join(body_parts)


# Keywords that often appear in verification URL paths
_VERIFY_KEYWORDS = re.compile(
    r"verifiz|confirm|bestätig|aktivier|register|email-confirm|e-mail-best|token",
    re.IGNORECASE,
)


def _find_link_in_body(body: str) -> Optional[str]:
    # Collect all URLs from body
    urls = re.findall(r"https?://[^\s<>\"'\\]+", body)
    for url in urls:
        if "kleinanzeigen" in url and _VERIFY_KEYWORDS.search(url):
            # Strip trailing punctuation / HTML artifacts
            url = re.sub(r'[>"\'\)\]]+$', "", url)
            return url
    # Broader fallback: any kleinanzeigen link
    for url in urls:
        if "kleinanzeigen" in url:
            url = re.sub(r'[>"\'\)\]]+$', "", url)
            return url
    return None


def fetch_verification_link(
    email_address: str,
    email_password: str,
    timeout: int = EMAIL_CHECK_TIMEOUT,
    imap_host: Optional[str] = None,
    imap_port: int = 993,
) -> Optional[str]:
    """
    Блокирующая функция — опрашивает IMAP ящик до timeout секунд.
    Возвращает ссылку верификации или None.
    """
    if not imap_host:
        imap_host, imap_port = _guess_imap(email_address)

    deadline = time.monotonic() + timeout
    poll_interval = 10  # секунд между попытками

    while time.monotonic() < deadline:
        try:
            with imaplib.IMAP4_SSL(imap_host, imap_port, timeout=20) as mail:
                mail.login(email_address, email_password)
                mail.select("INBOX")

                # Search senders
                for sender_pattern in (
                    'FROM "kleinanzeigen.de"',
                    'FROM "ebay-kleinanzeigen.de"',
                    'FROM "no-reply@kleinanzeigen.de"',
                    'SUBJECT "Kleinanzeigen"',
                ):
                    typ, data = mail.search(None, sender_pattern)
                    if typ == "OK" and data and data[0]:
                        msg_ids = data[0].split()
                        # Check the 10 most recent
                        for msg_id in reversed(msg_ids[-10:]):
                            typ2, msg_data = mail.fetch(msg_id, "(RFC822)")
                            if typ2 != "OK" or not msg_data:
                                continue
                            raw = msg_data[0][1]
                            msg = email.message_from_bytes(raw)
                            body = _extract_text(msg)
                            link = _find_link_in_body(body)
                            if link:
                                return link
        except (imaplib.IMAP4.error, OSError, TimeoutError):
            pass  # retry

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    return None


async def async_fetch_verification_link(
    email_address: str,
    email_password: str,
    timeout: int = EMAIL_CHECK_TIMEOUT,
    imap_host: Optional[str] = None,
    imap_port: int = 993,
) -> Optional[str]:
    """Асинхронная обёртка над fetch_verification_link."""
    return await asyncio.to_thread(
        fetch_verification_link,
        email_address,
        email_password,
        timeout,
        imap_host,
        imap_port,
    )
