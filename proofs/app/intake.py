"""Art intake (PRD 5.1) and the Readiness Report (5.2): the intake link,
the public form's uploads and answers, triage on every file, blocker
gating with an explicit override, and the shareable report link.

Files are stored under the account's artifact tree (the same
never-overwritten store as proof artifacts); AV scanning is recorded as
`skipped` until a clamd sidecar is deployed (PRD "File intake pipeline")
-- the scan status is on the record so nothing pretends otherwise.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, emailer, events, mails, proofs, storage, tokens, triage
from .db import Account, Contact, File, IntakeAnswer, Proof, TriageFinding, TriageReport, User, parse_ts, utcnow

MAX_FILE_BYTES = 400 * 1024 * 1024
ACCEPTED_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif", "svg", "pdf", "heic", "psd", "ai", "eps", "dst", "pes", "exp", "jef", "vp3")

# The default intake question set (PRD: each mapped to a proof field).
QUESTIONS = [
    ("garment_style_name", "What is this going on?", "text", "e.g. navy polo shirts, structured caps"),
    ("garment_color", "What colour is the item?", "text", "e.g. navy"),
    ("quantity", "How many?", "number", ""),
    ("size_breakdown", "Sizes, if it's clothing", "text", "e.g. S:4, M:10, L:8"),
    ("width_in", "How wide should the logo be, in inches?", "number", "3.5 for a left chest, 10 for a full front"),
    ("placement_name", "Where on the item?", "text", "e.g. left chest, cap front"),
    ("due_date", "When do you need it?", "date", ""),
    ("notes", "Anything else we should know?", "textarea", ""),
]


def send_intake(db: Session, proof: Proof, user: Optional[User], *, base_url: str = "", message: str = "") -> str:
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if proof.status in proofs.PROOF_TERMINAL:
        raise proofs.TransitionError("This proof is closed.")
    proofs.require_shop_name(account)
    if not contact.email:
        raise proofs.TransitionError("The customer needs an email address for an intake link.")
    tokens.revoke_for_proof(db, proof.id, purposes=("intake",))
    plaintext, t = tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose="intake",
                               days=max(proof.intake_window_days, 1))
    proof.status = "awaiting_art"
    proof.intake_expires_at = t.expires_at
    events.append(db, proof_id=proof.id, event_type="intake_sent", actor_type="user" if user else "system", actor_id=user.id if user else "",
                  token_hash=t.token_hash, payload={"to": contact.email, "expires_at": t.expires_at})
    proof.updated_at = utcnow()
    url = f"{base_url or config.PUBLIC_BASE_URL}/i/{plaintext}"
    subject, text, html = mails.intake_request(account, contact, proof, url, note=message, expires=t.expires_at[:10])
    if not emailer.send(to_email=contact.email, subject=subject, text=text, html=html, reply_to=account.reply_to_email):
        raise proofs.EmailFailed(url)
    return url


def store_file(db: Session, proof: Proof, *, data: bytes, filename: str, kind: str = "artwork", source: str = "intake",
               by_contact: bool = True, user: Optional[User] = None) -> File:
    if len(data) > MAX_FILE_BYTES:
        raise proofs.TransitionError(f"{filename} is over the 400 MB limit.")
    if not data:
        raise proofs.TransitionError(f"{filename} is empty.")
    sha = hashlib.sha256(data).hexdigest()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)[-80:] or "file"
    key = storage.put(f"accounts/{proof.account_id}/proofs/{proof.id}/uploads/{sha[:16]}-{safe}", data)
    f = File(account_id=proof.account_id, proof_id=proof.id, kind=kind, source=source, original_filename=filename, mime_type=triage.sniff(data, filename),
             bytes=len(data), storage_key=key, sha256=sha, scan_status="skipped", uploaded_by_contact=by_contact, uploaded_by_user_id=user.id if user else None)
    db.add(f)
    db.flush()
    return f


def run_triage(db: Session, proof: Proof, f: File, *, requested_width_mm: float, is_cap: bool = False, needle_count: int = 12) -> TriageReport:
    data = storage.get(f.storage_key)
    r = triage.analyze(data, f.original_filename, requested_width_mm=requested_width_mm, needle_count=needle_count, is_cap=is_cap)
    report = TriageReport(file_id=f.id, proof_id=proof.id, kind=r.kind, effective_ppi=r.effective_ppi, is_vector=r.is_vector, has_transparency=r.has_transparency,
                          color_count=r.color_count, colorspace=r.colorspace, min_feature_mm=r.min_feature_mm, requested_width_mm=requested_width_mm, report_json=r.to_json())
    db.add(report)
    db.flush()
    if r.preview_png:
        report.preview_storage_key = storage.put(f"accounts/{proof.account_id}/proofs/{proof.id}/uploads/{f.sha256[:16]}-preview.png", r.preview_png)
    for fd in r.findings:
        db.add(TriageFinding(triage_report_id=report.id, code=fd.code, severity=fd.severity, title=fd.title, message=fd.message,
                             measurement_json=json.dumps(fd.measurement), suggested_fix=fd.suggested_fix))
    db.flush()
    events.append(db, proof_id=proof.id, event_type="triage_completed", actor_type="system",
                  payload={"file_id": f.id, "kind": r.kind, "findings": [(x.code, x.severity) for x in r.findings], "effective_ppi": r.effective_ppi})
    return report


def receive_intake(db: Session, proof: Proof, *, token_hash: str, uploads: list[tuple[str, bytes]], answers: dict, ip: str, user_agent: str,
                   sms_consent: bool = False, phone: str = "") -> list[TriageReport]:
    """The customer submitted the intake form."""
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if proof.status in proofs.PROOF_TERMINAL:
        raise proofs.TransitionError("This job is closed.")
    kept = [(name, data) for name, data in uploads if data]
    if not kept:
        raise proofs.TransitionError("Please attach at least one artwork file.")
    for name, _ in kept:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ACCEPTED_EXTENSIONS:
            raise proofs.TransitionError(f"'{name}' isn't a file type we can use. PNG, JPG, SVG, PDF or a machine file, please.")
    # Answers land as data on the proof (PRD 5.1) and are kept verbatim.
    for field, value in answers.items():
        value = (value or "").strip()
        if not value:
            continue
        db.add(IntakeAnswer(proof_id=proof.id, field=field, value=value))
    width_in = _float(answers.get("width_in"))
    if width_in:
        proof.requested_width_mm = round(width_in * 25.4, 1)
    if answers.get("due_date"):
        proof.due_date = answers["due_date"].strip()[:10]
    if phone.strip():
        contact.phone = phone.strip()
    if sms_consent and phone.strip():
        contact.sms_opt_in_at = utcnow()
        contact.sms_opt_in_text = "I agree to receive text messages about this order from the shop. Message and data rates may apply. Reply STOP to opt out."
    reports = []
    is_cap = "cap" in (answers.get("garment_style_name") or "").lower() or "hat" in (answers.get("garment_style_name") or "").lower()
    for name, data in kept:
        f = store_file(db, proof, data=data, filename=name, by_contact=True)
        events.append(db, proof_id=proof.id, event_type="art_uploaded", actor_type="contact", actor_id=contact.id, token_hash=token_hash, ip=ip, user_agent=user_agent,
                      payload={"file_id": f.id, "filename": name, "bytes": len(data), "sha256": f.sha256})
        reports.append(run_triage(db, proof, f, requested_width_mm=proof.requested_width_mm, is_cap=is_cap))
    proof.status = "art_received"
    proof.updated_at = utcnow()
    tokens.revoke_for_proof(db, proof.id, purposes=("intake",))
    if account.reply_to_email:
        blockers = sum(len([x for x in r.findings if x.severity == "blocker"]) for r in reports)
        emailer.send(to_email=account.reply_to_email, subject=f"Artwork received: {proof.reference} {proof.title}",
                     text=f"{contact.display_name or contact.email} uploaded {len(kept)} file(s). Triage found {blockers} blocker(s). Open the proof to see the Readiness Report.")
    return reports


def attach_files(db: Session, proof: Proof, user: User, *, uploads: list[tuple[str, bytes]], requested_width_mm: float, is_cap: bool = False) -> list[TriageReport]:
    """The shop uploads on the customer's behalf (direct upload)."""
    reports = []
    for name, data in uploads:
        if not data:
            continue
        f = store_file(db, proof, data=data, filename=name, source="upload", by_contact=False, user=user)
        events.append(db, proof_id=proof.id, event_type="art_uploaded", actor_type="user", actor_id=user.id, payload={"file_id": f.id, "filename": name, "bytes": len(data), "sha256": f.sha256})
        if requested_width_mm:
            proof.requested_width_mm = requested_width_mm
        reports.append(run_triage(db, proof, f, requested_width_mm=proof.requested_width_mm, is_cap=is_cap))
    if reports and proof.status in ("draft", "awaiting_art", "intake_expired"):
        proof.status = "art_received"
    proof.updated_at = utcnow()
    return reports


def open_blockers(db: Session, proof: Proof) -> list[TriageFinding]:
    rows = db.execute(select(TriageFinding).join(TriageReport).where(TriageReport.proof_id == proof.id, TriageFinding.severity == "blocker", TriageFinding.status == "open")).scalars()
    return list(rows)


def override_blockers(db: Session, proof: Proof, user: User, *, reason: str) -> int:
    if not reason.strip():
        raise proofs.TransitionError("Say why you're overriding the blockers -- it goes on the record and the proof.")
    n = 0
    for fd in open_blockers(db, proof):
        fd.status = "overridden"
        fd.overridden_by = user.id
        fd.overridden_reason = reason.strip()
        fd.overridden_at = utcnow()
        n += 1
    proof.triage_override_reason = reason.strip()
    events.append(db, proof_id=proof.id, event_type="triage_overridden", actor_type="user", actor_id=user.id, payload={"reason": reason.strip(), "count": n})
    return n


def resolve_finding(db: Session, finding: TriageFinding, user: User) -> None:
    finding.status = "resolved"
    finding.overridden_by = user.id
    finding.overridden_at = utcnow()


def share_report(db: Session, proof: Proof, user: User, *, message: str, base_url: str = "") -> str:
    """A read-only link to the Readiness Report for the customer."""
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    reports = list(db.execute(select(TriageReport).where(TriageReport.proof_id == proof.id)).scalars())
    if not reports:
        raise proofs.TransitionError("Nothing to share yet -- no artwork has been triaged.")
    for r in reports:
        r.shop_message = message.strip()
    plaintext, t = tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose="triage_report", version_scope="all_read_only")
    events.append(db, proof_id=proof.id, event_type="triage_report_sent", actor_type="user", actor_id=user.id, token_hash=t.token_hash, payload={"to": contact.email})
    url = f"{base_url or config.PUBLIC_BASE_URL}/r/{plaintext}"
    shop = account.shop_name or "Your embroiderer"
    emailer.send(to_email=contact.email, subject=f"{shop}: what we found in your artwork",
                 text=f"Hi {contact.display_name or 'there'},\n\nWe looked at your artwork for {proof.title}. Here's what we found and what it means for embroidery:\n{url}\n\n"
                      + (message.strip() + "\n\n" if message.strip() else "") + f"{shop}", reply_to=account.reply_to_email)
    return url


def expire_intakes(db: Session) -> int:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    n = 0
    for p in db.execute(select(Proof).where(Proof.status == "awaiting_art")).scalars():
        if p.intake_expires_at and parse_ts(p.intake_expires_at) < now:
            p.status = "intake_expired"
            events.append(db, proof_id=p.id, event_type="intake_expired", actor_type="system")
            n += 1
    return n


def _float(v) -> float:
    try:
        return float(str(v or "").strip() or 0)
    except ValueError:
        return 0.0
