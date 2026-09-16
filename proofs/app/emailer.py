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
        r = httpx.post("https://api.postmarkapp.com/email", json=body, headers={"X-Postmark-Server-Token": config.POSTMARK_API_TOKEN, "Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            log.error("Postmark refused %s: %s", to_email, r.text[:200])
            return False
        return True
    if not config.SMTP_HOST:
        log.warning("No email transport configured; dropping mail to %s (%s)", to_email, subject)
        return False
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
        log.error("SMTP send to %s failed: %s", to_email, e)
        return False
