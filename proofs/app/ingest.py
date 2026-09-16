"""Email ingest -- PRD 5.1 "Email ingest". Every account gets
art@{slug}.piperstitch.com (or art+{slug}@ the inbound domain). The
provider (Postmark inbound) posts the parsed message as JSON; the rules
here are the PRD's, all mandatory:

* reject anything that fails DKIM or carries dmarc=fail
* a trusted sender (an account user, or an existing contact's address)
  creates a proof in art_received with the attachments triaged and the
  body kept as the first note
* an unknown sender lands in quarantine; nothing is auto-created
* 50 messages per account per hour, 10 per sending address per hour;
  overflow is held and the shop alerted
* signature images and tracking pixels are dropped by size
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, emailer, events, intake, proofs, storage
from .db import Account, AccountUser, Contact, InboundEmail, Proof, User, parse_ts, utcnow

INBOUND_DOMAIN_SUFFIX = ".piperstitch.com"
SIGNATURE_IMAGE_MAX_BYTES = 15 * 1024
ACCOUNT_HOURLY_LIMIT = 50
SENDER_HOURLY_LIMIT = 10
RESERVED_SLUGS = {"www", "app", "api", "mail", "admin", "proofs", "support", "help", "billing"}


def make_slug(db: Session, account: Account) -> str:
    if account.slug:
        return account.slug
    base = re.sub(r"[^a-z0-9]+", "-", (account.shop_name or "shop").lower()).strip("-")[:30] or "shop"
    candidate = base
    n = 2
    while candidate in RESERVED_SLUGS or _slug_taken(db, candidate):
        candidate = f"{base}-{n}"
        n += 1
    account.slug = candidate
    return candidate


def _slug_taken(db: Session, slug: str) -> bool:
    for a in db.execute(select(Account).where(Account.slug.isnot(None))).scalars():
        s = a.slug or ""
        # Near-miss slugs are refused too (PRD): same letters ignoring dashes/digits.
        if s == slug or re.sub(r"[-0-9]", "", s) == re.sub(r"[-0-9]", "", slug):
            return True
    return False


def slug_from_address(address: str) -> Optional[str]:
    """art@{slug}.piperstitch.com or art+{slug}@anything."""
    a = (address or "").strip().lower()
    m = re.search(r"<([^>]+)>", a)
    if m:
        a = m.group(1)
    m = re.match(r"^art\+([a-z0-9-]+)@", a)
    if m:
        return m.group(1)
    m = re.match(r"^art@([a-z0-9-]+)" + re.escape(INBOUND_DOMAIN_SUFFIX) + r"$", a)
    if m:
        return m.group(1)
    return None


def _auth_ok(headers: list[dict]) -> tuple[bool, str]:
    """DKIM must pass and DMARC must not fail, per the message's own
    Authentication-Results header (added by the receiving provider)."""
    results = " ".join(str(h.get("Value", "")) for h in headers if str(h.get("Name", "")).lower() == "authentication-results").lower()
    if not results:
        return False, "no authentication results"
    if "dmarc=fail" in results:
        return False, "dmarc=fail"
    if "dkim=pass" not in results:
        return False, "dkim did not pass"
    return True, results[:200]


def _rate_limited(db: Session, account_id: str, sender: str, now: datetime) -> Optional[str]:
    since = (now - timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = list(db.execute(select(InboundEmail).where(InboundEmail.account_id == account_id, InboundEmail.received_at >= since)).scalars())
    # `rows` includes the message being received.
    if len(rows) > ACCOUNT_HOURLY_LIMIT:
        return "account limit (50/hour)"
    if len([r for r in rows if r.from_email == sender]) > SENDER_HOURLY_LIMIT:
        return "sender limit (10/hour)"
    return None


def _small_pixels(data: bytes, limit: int = 400) -> bool:
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        return im.width < limit and im.height < limit
    except Exception:  # noqa: BLE001
        return True


def _trusted(db: Session, account: Account, sender: str) -> Optional[Contact]:
    """A contact record to attach the proof to when the sender is
    trusted; a synthetic one for an account user forwarding a customer's
    mail (the proof's contact is then filled from the body if possible)."""
    for m in db.execute(select(AccountUser).where(AccountUser.account_id == account.id, AccountUser.disabled_at.is_(None))).scalars():
        if m.user.email == sender:
            return Contact(account_id=account.id, display_name="", email="")   # forwarded by the shop; contact set later
    c = db.execute(select(Contact).where(Contact.account_id == account.id, Contact.email == sender, Contact.archived_at.is_(None))).scalar_one_or_none()
    return c


def _forwarded_customer(body: str) -> tuple[str, str]:
    """From a forwarded mail's body: the original sender's name and address."""
    m = re.search(r"From:\s*(?:\"?([^\"<\n]*)\"?\s*)?<?([\w.+-]+@[\w.-]+\.\w+)>?", body or "")
    if m:
        return (m.group(1) or "").strip(), m.group(2).strip().lower()
    return "", ""


def receive(db: Session, payload: dict, *, now: Optional[datetime] = None) -> InboundEmail:
    """Postmark inbound JSON -> an InboundEmail row, and a proof when trusted."""
    now = now or datetime.now(timezone.utc)
    to_raw = str(payload.get("OriginalRecipient") or payload.get("To") or "")
    slug = slug_from_address(to_raw) or (payload.get("MailboxHash") or "").lower() or None
    from_full = payload.get("FromFull") or {}
    sender = str(from_full.get("Email") or payload.get("From") or "").strip().lower()
    m = re.search(r"<([^>]+)>", sender)
    if m:
        sender = m.group(1)
    row = InboundEmail(provider_message_id=str(payload.get("MessageID") or ""), from_email=sender, from_name=str(from_full.get("Name") or ""),
                       to_address=to_raw, subject=str(payload.get("Subject") or "")[:300], body_text=str(payload.get("TextBody") or payload.get("StrippedTextReply") or "")[:20000],
                       received_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    db.add(row)
    db.flush()
    account = db.execute(select(Account).where(Account.slug == slug)).scalar_one_or_none() if slug else None
    if account is None:
        row.status, row.reason = "rejected", "unknown address"
        return row
    row.account_id = account.id
    ok, why = _auth_ok(payload.get("Headers") or [])
    row.auth_result = why
    if not ok:
        row.status, row.reason = "rejected", f"authentication: {why}"
        return row
    limited = _rate_limited(db, account.id, sender, now)
    if limited:
        row.status, row.reason = "rate_limited", limited
        if account.reply_to_email:
            emailer.send(to_email=account.reply_to_email, subject="Incoming art email held (rate limit)", text=f"A message from {sender} was held: {limited}. It's in your inbound queue.")
        return row
    # Attachments: store now (quarantine or not), drop signature-sized images.
    kept = []
    for att in payload.get("Attachments") or []:
        name = str(att.get("Name") or "attachment")
        try:
            data = base64.b64decode(att.get("Content") or "")
        except (ValueError, TypeError):
            continue
        mime = str(att.get("ContentType") or "")
        if not data:
            continue
        if mime.startswith("image/") and len(data) < SIGNATURE_IMAGE_MAX_BYTES and _small_pixels(data):
            continue   # signature image / tracking pixel
        key = storage.put(f"accounts/{account.id}/inbound/{row.id}/{re.sub(r'[^A-Za-z0-9._-]', '_', name)[-80:]}", data)
        kept.append({"name": name, "storage_key": key, "bytes": len(data), "mime": mime})
    row.attachments_json = json.dumps(kept)
    contact = _trusted(db, account, sender)
    if contact is None:
        row.status, row.reason = "quarantined", "unknown sender"
        if account.reply_to_email:
            emailer.send(to_email=account.reply_to_email, subject=f"Art email waiting to be accepted: {row.subject or '(no subject)'}",
                         text=f"From {sender} with {len(kept)} attachment(s). Accept or reject it on your board.")
        return row
    _accept(db, row, account, contact, user=None)
    return row


def _accept(db: Session, row: InboundEmail, account: Account, contact: Contact, *, user: Optional[User]) -> Proof:
    if contact.id is None or not contact.email:
        name, email = _forwarded_customer(row.body_text)
        contact = proofs.find_or_create_contact(db, account, display_name=name or row.from_name, email=email or row.from_email)
    title = re.sub(r"^(re|fwd?|fw):\s*", "", row.subject, flags=re.I).strip()[:120] or "Emailed artwork"
    p = proofs.create_proof(db, account, user, contact=contact, title=title)
    if row.body_text.strip():
        from .db import Message
        db.add(Message(proof_id=p.id, direction="inbound", author_type="contact", author_id=contact.id, body=row.body_text.strip()[:5000]))
    uploads = [(a["name"], storage.get(a["storage_key"])) for a in json.loads(row.attachments_json or "[]")]
    if uploads:
        for name, data in uploads:
            f = intake.store_file(db, p, data=data, filename=name, source="email", by_contact=user is None, user=user)
            events.append(db, proof_id=p.id, event_type="art_uploaded", actor_type="contact" if user is None else "user", actor_id=contact.id if user is None else user.id,
                          payload={"file_id": f.id, "filename": name, "bytes": len(data), "sha256": f.sha256, "via": "email"})
            intake.run_triage(db, p, f, requested_width_mm=0)
        first = json.loads(row.attachments_json or "[]")
        if first and not p.core_project_id:
            from .db import File as _File
            f0 = db.execute(select(_File).where(_File.proof_id == p.id).order_by(_File.uploaded_at)).scalars().first()
            if f0 is not None:
                intake.digitize_into_core(db, p, f0)
        p.status = "art_received"
    row.status, row.proof_id, row.decided_at = "accepted", p.id, utcnow()
    row.decided_by = user.id if user else None
    proofs._refresh(db, p)
    return p


def accept(db: Session, row: InboundEmail, user: User) -> Proof:
    account = db.get(Account, row.account_id)
    if row.status not in ("quarantined", "rate_limited"):
        raise proofs.TransitionError("This message was already handled.")
    contact = proofs.find_or_create_contact(db, account, display_name=row.from_name, email=row.from_email)
    return _accept(db, row, account, contact, user=user)


def reject(db: Session, row: InboundEmail, user: User) -> None:
    row.status, row.reason, row.decided_at, row.decided_by = "rejected", "rejected by the shop", utcnow(), user.id


def queue(db: Session, account_id: str) -> list[InboundEmail]:
    return list(db.execute(select(InboundEmail).where(InboundEmail.account_id == account_id, InboundEmail.status.in_(("quarantined", "rate_limited"))).order_by(InboundEmail.received_at.desc())).scalars())
