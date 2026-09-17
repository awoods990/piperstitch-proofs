"""Outbound email -- Microsoft 365 SMTP or Postmark, exactly as License
Admin sends, plus an outbox directory for development and tests."""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from . import config
from .db import utcnow

log = logging.getLogger("proofs.email")

# Why the last send failed, in the words of the transport ("SendAsDenied",
# "Sender signature not defined") -- what the owner needs to read to fix
# the settings, so the Settings page's test button shows it.
last_error = ""


def transport() -> str:
    """Which way mail leaves this server: outbox, postmark, smtp or none."""
    if config.EMAIL_OUTBOX_DIR:
        return "outbox"
    if config.POSTMARK_API_TOKEN:
        return "postmark"
    if config.SMTP_HOST:
        return "smtp"
    return "none"


def from_address() -> str:
    return (config.POSTMARK_FROM or config.SMTP_FROM) if transport() == "postmark" else config.SMTP_FROM


def describe() -> dict:
    """Non-secret summary for /health and the Settings page."""
    d = {"transport": transport(), "from": from_address()}
    if transport() == "smtp":
        d["host"] = f"{config.SMTP_HOST}:{config.SMTP_PORT}"
        d["username"] = config.SMTP_USERNAME
    if transport() == "postmark":
        d["stream"] = config.POSTMARK_MESSAGE_STREAM
    return d


def _fail(message: str) -> bool:
    global last_error
    last_error = message
    log.error("%s", message)
    return False


def send(*, to_email: str, subject: str, text: str, html: str = "", reply_to: str = "") -> bool:
    if config.EMAIL_OUTBOX_DIR:
        out = Path(config.EMAIL_OUTBOX_DIR)
        out.mkdir(parents=True, exist_ok=True)
        record = {"to": to_email, "subject": subject, "text": text, "html": html, "reply_to": reply_to, "at": utcnow()}
        import time
        (out / f"{time.time_ns():020d}.json").write_text(json.dumps(record, indent=2))
        return True
    if config.POSTMARK_API_TOKEN:
        body = {"From": config.POSTMARK_FROM or config.SMTP_FROM, "To": to_email, "Subject": subject, "TextBody": text,
                "MessageStream": config.POSTMARK_MESSAGE_STREAM}
        if html:
            body["HtmlBody"] = html
        if reply_to:
            body["ReplyTo"] = reply_to
        try:
            r = httpx.post("https://api.postmarkapp.com/email", json=body, headers={"X-Postmark-Server-Token": config.POSTMARK_API_TOKEN, "Accept": "application/json"}, timeout=30)
        except httpx.HTTPError as e:
            return _fail(f"Postmark unreachable for the message to {to_email}: {e}")
        if r.status_code != 200:
            return _fail(f"Postmark refused the message to {to_email} (HTTP {r.status_code}): {r.text[:300]}")
        return True
    if not config.SMTP_HOST:
        return _fail(f"No email transport configured (set SMTP_HOST or POSTMARK_API_TOKEN); dropped mail to {to_email} ({subject})")
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if config.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
            server.starttls()
        with server:
            if config.SMTP_USERNAME:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        return _fail(f"SMTP send to {to_email} via {config.SMTP_HOST}:{config.SMTP_PORT} as {config.SMTP_FROM} failed: {e}")


def send_test(to_email: str, shop_name: str) -> str:
    """Send one plain message and return "" on success or the transport's
    reason on failure -- the Settings page's "Send a test email" button."""
    global last_error
    last_error = ""
    ok = send(to_email=to_email, subject=f"PiperStitch Proofs test email for {shop_name}",
              text=f"This is a test from PiperStitch Proofs. If you can read it, proofs, artwork requests and certificates from {shop_name} will reach your customers.\n\nSent via {transport()} as {from_address()}.")
    return "" if ok else (last_error or "The email could not be sent.")
