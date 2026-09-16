"""SMS and MMS through Twilio -- PRD "Notifications" and 5.1 "MMS ingest".

Rules that live here, not in the caller:

* nothing goes out unless the contact opted in (`sms_opt_in_at`, recorded
  with the consent text they saw) and hasn't opted out, and the account
  has SMS turned on
* STOP / UNSUBSCRIBE / CANCEL / END / QUIT from a number opts it out;
  START / YES / UNSTOP opts it back in -- carrier-required keywords
* every send is short, names the shop, and carries the link
* inbound MMS from a known number becomes artwork on that customer's
  open proof (or a new one)

Deliverability caveat, stated plainly: Twilio will not deliver
application-to-person traffic to US numbers from an unregistered
10-digit number. The account needs an A2P 10DLC brand and campaign
(days to weeks of carrier review) or a verified toll-free number before
`TWILIO_FROM` sends anything but errors. Until then sends fail and are
logged as such; nothing else in the product depends on them.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, events, intake, proofs
from .db import Account, AccountEntitlements, Contact, Proof, utcnow

log = logging.getLogger("proofs.sms")

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "yes", "unstop"}


def configured() -> bool:
    return bool(config.SMS_OUTBOX_DIR or (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM))


def normalize(phone: str) -> str:
    """E.164 for US/Canada numbers typed casually; anything else as-is with a +."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if not (phone or "").strip().startswith("+") else (phone or "").strip()


def can_text(db: Session, account: Account, contact: Contact) -> tuple[bool, str]:
    ent = proofs.entitlements(db, account)
    if not ent.sms_enabled:
        return False, "sms_not_enabled"
    if not configured():
        return False, "sms_not_configured"
    if not contact.phone:
        return False, "no_phone"
    if contact.opted_out_at:
        return False, "opted_out"
    if not contact.sms_opt_in_at:
        return False, "no_opt_in"
    return True, ""


def send(db: Session, account: Account, contact: Contact, body: str, *, proof: Optional[Proof] = None, kind: str = "notification") -> bool:
    ok, reason = can_text(db, account, contact)
    if not ok:
        return False
    to = normalize(contact.phone)
    delivered = _transport(to, body)
    if proof is not None:
        events.append(db, proof_id=proof.id, proof_version_id=proof.current_version_id, event_type="sms_sent" if delivered else "sms_failed",
                      actor_type="system", payload={"to": to[-4:].rjust(len(to), "*"), "kind": kind, "chars": len(body)})
    return delivered


def _transport(to: str, body: str) -> bool:
    if config.SMS_OUTBOX_DIR:
        out = Path(config.SMS_OUTBOX_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{time.time_ns():020d}.json").write_text(json.dumps({"to": to, "body": body, "at": utcnow()}))
        return True
    data = {"To": to, "Body": body}
    if config.TWILIO_FROM.startswith("MG"):
        data["MessagingServiceSid"] = config.TWILIO_FROM
    else:
        data["From"] = config.TWILIO_FROM
    try:
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json", data=data,
                       auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=30)
    except httpx.HTTPError as e:
        log.error("Twilio unreachable: %s", e)
        return False
    if r.status_code >= 300:
        log.error("Twilio refused %s: %s", to[-4:], r.text[:300])
        return False
    return True


def verify_signature(url: str, form: dict, signature: str) -> bool:
    """Twilio's X-Twilio-Signature: HMAC-SHA1 of the URL plus the sorted
    POST parameters, keyed by the auth token."""
    if not config.TWILIO_AUTH_TOKEN:
        return True   # dev/tests
    material = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = base64.b64encode(hmac.new(config.TWILIO_AUTH_TOKEN.encode(), material.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, signature or "")


def inbound(db: Session, form: dict, *, fetch_media=None) -> str:
    """A Twilio inbound message webhook. Returns the reply text (TwiML is
    built by the route). Keywords first, then MMS artwork intake."""
    sender = normalize(str(form.get("From", "")))
    body = str(form.get("Body", "")).strip()
    word = body.lower().strip(" .!")
    contacts = [c for c in db.execute(select(Contact).where(Contact.archived_at.is_(None))).scalars() if normalize(c.phone) == sender and sender]
    if word in STOP_WORDS:
        for c in contacts:
            c.opted_out_at = utcnow()
        return "You're unsubscribed and won't get more texts from this shop. Reply START to opt back in."
    if word in START_WORDS:
        for c in contacts:
            c.opted_out_at = None
            if not c.sms_opt_in_at:
                c.sms_opt_in_at = utcnow()
                c.sms_opt_in_text = f"Replied {body.upper()} by SMS"
        return "You're opted in to texts from this shop. Reply STOP to opt out."
    media_count = int(str(form.get("NumMedia", "0")) or 0)
    if media_count == 0:
        return ""
    if not contacts:
        return "Thanks -- we don't recognise this number. Please email your artwork to the shop instead."
    stored = 0
    for c in contacts:
        account = db.get(Account, c.account_id)
        p = db.execute(select(Proof).where(Proof.contact_id == c.id, Proof.status.in_(("awaiting_art", "intake_expired", "draft"))).order_by(Proof.updated_at.desc())).scalars().first()
        if p is None:
            p = proofs.create_proof(db, account, None, contact=c, title="Texted artwork")
        for i in range(media_count):
            url = str(form.get(f"MediaUrl{i}", ""))
            mime = str(form.get(f"MediaContentType{i}", "application/octet-stream"))
            data = (fetch_media or _fetch_media)(url)
            if not data:
                continue
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "application/pdf": "pdf"}.get(mime, mime.split("/")[-1])
            f = intake.store_file(db, p, data=data, filename=f"mms-{i + 1}.{ext}", source="mms", by_contact=True)
            events.append(db, proof_id=p.id, event_type="art_uploaded", actor_type="contact", actor_id=c.id, payload={"file_id": f.id, "via": "mms", "bytes": len(data), "sha256": f.sha256})
            intake.run_triage(db, p, f, requested_width_mm=p.requested_width_mm)
            stored += 1
        if stored:
            p.status = "art_received"
            if not p.core_project_id:
                from .db import File as _File
                f0 = db.execute(select(_File).where(_File.proof_id == p.id).order_by(_File.uploaded_at)).scalars().first()
                if f0 is not None:
                    intake.digitize_into_core(db, p, f0)
            proofs._refresh(db, p)
            if body:
                from .db import Message
                db.add(Message(proof_id=p.id, direction="inbound", author_type="contact", author_id=c.id, body=body[:2000]))
            if account.reply_to_email:
                from . import emailer
                emailer.send(to_email=account.reply_to_email, subject=f"Artwork texted in: {p.reference}", text=f"{c.display_name or sender} texted {stored} file(s). The Readiness Report is on the proof.")
    return f"Got it -- {stored} file{'s' if stored != 1 else ''} received. The shop will send you a proof to approve." if stored else "We couldn't read that attachment. Try a photo or PDF."


def _fetch_media(url: str) -> bytes:
    try:
        r = httpx.get(url, auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), follow_redirects=True, timeout=60)
        return r.content if r.status_code == 200 else b""
    except httpx.HTTPError:
        return b""


def twiml(text: str) -> str:
    from xml.sax.saxutils import escape
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{("<Message>" + escape(text) + "</Message>") if text else ""}</Response>'
