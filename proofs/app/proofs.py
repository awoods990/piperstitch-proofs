"""The proof lifecycle -- PRD "Proof lifecycle state machine" -- as plain
functions over the database. Routes call these; tests call these. Every
transition appends to the evidence chain (`events`) and re-derives the
proof's rollup status. Nothing here renders HTML.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, core_client, emailer, events, garments, pdfgen, stitch, storage, texts, tokens
from .db import (Account, AccountEntitlements, AccountUser, ApprovalRecord, ChangeRequest, Contact, Message, Proof, ProofVersion,
                 TermsVersion, ThreadStop, User, parse_ts, utcnow)

log = logging.getLogger("proofs")

VERSION_LIVE = ("sent", "viewed", "changes_requested", "approved", "approved_with_notes")
VERSION_TERMINAL = ("superseded",)
PROOF_TERMINAL = ("completed", "void")


class TransitionError(Exception):
    """A guard failed. The message is safe to show the shop."""


class Forbidden(Exception):
    pass


# --- helpers ---------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def current_terms(db: Session, account: Account) -> TermsVersion:
    t = db.execute(select(TermsVersion).where(TermsVersion.account_id == account.id, TermsVersion.is_current.is_(True))).scalar_one_or_none()
    if t is None:
        t = TermsVersion(account_id=account.id, label="v1", body=texts.DEFAULT_TERMS, consent_text=texts.CONSENT_TEXT, is_current=True)
        db.add(t)
        db.flush()
    return t


def set_terms(db: Session, account: Account, *, body: str, consent_text: str) -> TermsVersion:
    if texts.CONSENT_MINIMUM not in consent_text:
        raise TransitionError("The consent text must keep the required sentence: “" + texts.CONSENT_MINIMUM + "”")
    previous = list(db.execute(select(TermsVersion).where(TermsVersion.account_id == account.id, TermsVersion.is_current.is_(True))).scalars())
    n = len(list(db.execute(select(TermsVersion).where(TermsVersion.account_id == account.id)).scalars())) + 1
    for p in previous:
        p.is_current = False
    t = TermsVersion(account_id=account.id, label=f"v{n}", body=body, consent_text=consent_text, is_current=True)
    db.add(t)
    db.flush()
    return t


def entitlements(db: Session, account: Account) -> AccountEntitlements:
    e = db.get(AccountEntitlements, account.id)
    if e is None:
        e = AccountEntitlements(account_id=account.id)
        db.add(e)
        db.flush()
    return e


def rollup_status(db: Session, proof: Proof) -> str:
    """PRD 'Two scopes': the proof's status equals a proof-scoped state
    or its current version's status. Derived, never hand-set."""
    if proof.status in ("void", "completed", "released"):
        return proof.status
    if proof.status == "internal_review":
        return proof.status
    if proof.status in ("awaiting_art", "intake_expired", "art_received") and not proof.current_version_id:
        return proof.status
    if proof.current_version_id:
        v = db.get(ProofVersion, proof.current_version_id)
        if v is not None:
            return v.status
    return "digitizing" if proof.core_project_id else "draft"


def _refresh(db: Session, proof: Proof) -> None:
    proof.status = rollup_status(db, proof)
    proof.updated_at = utcnow()


def next_reference(db: Session, account: Account) -> str:
    n = account.next_reference
    account.next_reference = n + 1
    return f"PF-{n}"


# --- contacts and proofs ----------------------------------------------------------

def find_or_create_contact(db: Session, account: Account, *, display_name: str, email: str, company_name: str = "", phone: str = "") -> Contact:
    email = (email or "").strip().lower()
    c = None
    if email:
        c = db.execute(select(Contact).where(Contact.account_id == account.id, Contact.email == email, Contact.archived_at.is_(None))).scalar_one_or_none()
    if c is None:
        c = Contact(account_id=account.id, display_name=display_name.strip(), email=email, company_name=company_name.strip(), phone=phone.strip())
        db.add(c)
        db.flush()
    else:
        if display_name.strip():
            c.display_name = display_name.strip()
        if company_name.strip():
            c.company_name = company_name.strip()
        if phone.strip():
            c.phone = phone.strip()
    return c


def create_proof(db: Session, account: Account, user: Optional[User], *, contact: Contact, title: str,
                 core_project_id: Optional[str] = None, core_project_name: str = "", due_date: Optional[str] = None) -> Proof:
    p = Proof(account_id=account.id, contact_id=contact.id, reference=next_reference(db, account), title=title.strip() or "Untitled",
              core_project_id=core_project_id, core_project_name=core_project_name, due_date=due_date,
              response_window_days=account.default_response_window_days, revisions_included=account.default_revisions_included,
              created_by=user.id if user else None)
    db.add(p)
    db.flush()
    events.append(db, proof_id=p.id, event_type="created", actor_type="user" if user else "system", actor_id=user.id if user else "",
                  payload={"reference": p.reference, "title": p.title, "contact_id": contact.id})
    if core_project_id:
        events.append(db, proof_id=p.id, event_type="project_linked", actor_type="user" if user else "system", actor_id=user.id if user else "",
                      payload={"core_project_id": core_project_id, "core_project_name": core_project_name})
    _refresh(db, p)
    return p


def link_project(db: Session, proof: Proof, user: Optional[User], *, core_project_id: str, core_project_name: str) -> None:
    if proof.status in PROOF_TERMINAL:
        raise TransitionError("This proof is closed.")
    proof.core_project_id = core_project_id
    proof.core_project_name = core_project_name
    events.append(db, proof_id=proof.id, event_type="project_linked", actor_type="user" if user else "system", actor_id=user.id if user else "",
                  payload={"core_project_id": core_project_id, "core_project_name": core_project_name})
    _refresh(db, proof)


# --- compose ----------------------------------------------------------------------

ARTIFACT_KEYS = ("pdf", "render", "hero", "social")


def _artifact_key(proof: Proof, version_number: int, name: str) -> str:
    return f"accounts/{proof.account_id}/proofs/{proof.id}/v{version_number}/{name}"


def _size_breakdown_from_form(raw: str) -> dict:
    """'S:5, M:10, L:8' or JSON -> {size: count}."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return {str(k): int(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError):
            return {}
    out: dict = {}
    for part in raw.replace(";", ",").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
        elif "x" in part.lower():
            k, v = part.lower().split("x", 1)
        else:
            continue
        try:
            out[k.strip().upper()] = int(v.strip())
        except ValueError:
            continue
    return out


def compose_version(db: Session, proof: Proof, user: Optional[User], *, document: dict, garment_style_name: str = "",
                    garment_color: str = "", placement_name: str = "", placement_notes: str = "", quantity: int = 0,
                    size_breakdown: Optional[dict] = None, message_body: str = "", price_line: str = "", hoop_code: str = "",
                    garment_template_id: str = "", garment_zone: str = "", placement_down_mm: float = 0, placement_across_mm: float = 0,
                    stitch_client: Optional[core_client.StitchClient] = None) -> ProofVersion:
    """Build version N from a Core document: digitize, analyse, render,
    export every machine file, produce the PDF, hash all of it, copy the
    conditions onto the version. Immutable from here (PRD invariant 1)."""
    if proof.status in PROOF_TERMINAL:
        raise TransitionError("This proof is closed.")
    account = db.get(Account, proof.account_id)
    client = stitch_client or core_client.stitch
    digitized = client.digitize(document)
    analysis = stitch.analyze(digitized)
    if analysis.stitch_count == 0:
        raise TransitionError("The design has no stitches -- nothing to proof.")

    existing = list(db.execute(select(ProofVersion).where(ProofVersion.proof_id == proof.id).order_by(ProofVersion.version_number)).scalars())
    n = (existing[-1].version_number + 1) if existing else 1
    previous = existing[-1] if existing else None
    terms = current_terms(db, account)
    fabric = (document.get("objects") or [{}])[0].get("parameters", {}).get("fabricType", "standard") if document.get("objects") else "standard"
    template = garments.TEMPLATE_BY_ID.get(garment_template_id) if garment_template_id else None
    zone = template.zone(garment_zone or template.default_zone) if template else None
    if template and not placement_name.strip():
        placement_name = zone.label
    if template and not placement_notes.strip():
        placement_notes = garments.measured_note(template, zone, analysis.height_mm, placement_down_mm, placement_across_mm, account.units)

    v = ProofVersion(
        proof_id=proof.id, version_number=n, status="ready_to_send", design_hash=analysis.design_hash,
        stitch_count=analysis.stitch_count, color_change_count=analysis.color_change_count, trim_count=analysis.trim_count,
        width_mm=analysis.width_mm, height_mm=analysis.height_mm, estimated_run_seconds=analysis.estimated_run_seconds,
        fabric_code=fabric, hoop_code=hoop_code, stabilizer_advice=texts.STABILIZER_ADVICE.get(fabric, ""),
        garment_style_name=garment_style_name.strip() or (template.name if template else ""), garment_color=garment_color.strip(), placement_name=placement_name.strip(),
        placement_notes=placement_notes.strip(), quantity=int(quantity or 0), size_breakdown_json=json.dumps(size_breakdown or {}),
        garment_template_id=template.id if template else "", garment_zone=zone.id if zone else "",
        placement_down_mm=float(placement_down_mm or 0), placement_across_mm=float(placement_across_mm or 0),
        price_line=price_line.strip(), terms_version_id=terms.id, message_body=message_body.strip(),
        document_json=stitch.canonical_json(document), composed_by=user.id if user else None,
    )
    db.add(v)
    db.flush()
    for s in analysis.stops:
        db.add(ThreadStop(proof_version_id=v.id, stop_number=s.stop_number, thread_brand=s.thread_brand, thread_code=s.thread_code,
                          thread_name=s.thread_name, hex=s.hex, stitch_count=s.stitch_count))
    db.flush()

    # Artifacts, atomically at creation, each hashed (PRD "Formats produced").
    render = stitch.render_png(digitized)
    mockup = diagram = b""
    if template:
        render_ppm = stitch.render_pixels_per_mm(render, analysis)
        mockup = garments.composite(render, render_ppm, template, zone, garments.color_hex(garment_color), offsets_mm=(placement_down_mm, placement_across_mm))
        diagram = garments.placement_diagram(template, zone, analysis.width_mm, analysis.height_mm, placement_down_mm, placement_across_mm, account.units)
    hero = stitch.fit_into(mockup or render, (1200, 630))
    social = stitch.fit_into(mockup or render, (1080, 1080))
    machine: dict[str, bytes] = {fmt: client.export(document, fmt) for fmt in core_client.MACHINE_FORMATS}
    contact = db.get(Contact, proof.contact_id)
    pdf = pdfgen.proof_pdf(
        shop_name=account.shop_name or "Your embroiderer", reference=proof.reference, title=proof.title, version_number=n, version_count=n,
        composed_at=v.composed_at, contact_name=contact.display_name or contact.email, render_png=render, width_mm=v.width_mm, height_mm=v.height_mm,
        stitch_count=v.stitch_count, color_change_count=v.color_change_count, trim_count=v.trim_count,
        stops=[stop_dict(s) for s in v.thread_stops], garment=v.garment_style_name, garment_color=v.garment_color,
        placement=v.placement_name, placement_notes=v.placement_notes, quantity=v.quantity, size_breakdown=v.size_breakdown,
        fabric_name=texts.FABRIC_NAMES.get(fabric, fabric), stabilizer=v.stabilizer_advice, terms_body=terms.body, message=v.message_body,
        price_line=v.price_line, design_hash=v.design_hash, supersedes=previous.version_number if previous else None,
        mockup_png=mockup or None, diagram_png=diagram or None,
    )
    hashes = {"pdf": stitch.sha256(pdf), "render": stitch.sha256(render), "hero": stitch.sha256(hero), "social": stitch.sha256(social),
              "mockup": stitch.sha256(mockup) if mockup else "", "diagram": stitch.sha256(diagram) if diagram else "",
              "machine_files": {fmt: stitch.sha256(data) for fmt, data in machine.items()}}
    storage.put(_artifact_key(proof, n, "proof.pdf"), pdf)
    storage.put(_artifact_key(proof, n, "render.png"), render)
    if mockup:
        storage.put(_artifact_key(proof, n, "mockup.png"), mockup)
        storage.put(_artifact_key(proof, n, "diagram.png"), diagram)
    storage.put(_artifact_key(proof, n, "hero.png"), hero)
    storage.put(_artifact_key(proof, n, "social.png"), social)
    for fmt, data in machine.items():
        storage.put(_artifact_key(proof, n, f"design.{fmt}"), data)
    v.artifact_hashes_json = json.dumps(hashes, sort_keys=True)

    if previous is not None:
        v.change_summary = describe_changes(previous, v)
    proof.current_version_id = v.id
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="composed", actor_type="user" if user else "system",
                  actor_id=user.id if user else "", payload={"version": n, "design_hash": v.design_hash, "artifact_hashes": hashes,
                                                              "stitch_count": v.stitch_count, "width_mm": v.width_mm, "height_mm": v.height_mm})
    _refresh(db, proof)
    return v


def stop_dict(s: ThreadStop) -> dict:
    return {"stop_number": s.stop_number, "thread_brand": s.thread_brand, "thread_code": s.thread_code, "thread_name": s.thread_name,
            "hex": s.hex, "stitch_count": s.stitch_count}


def describe_changes(a: ProofVersion, b: ProofVersion) -> str:
    """The 'what changed' line on a new version (PRD 5.5)."""
    parts = []
    if a.design_hash != b.design_hash:
        parts.append("the design changed")
    if abs(a.width_mm - b.width_mm) > 0.05 or abs(a.height_mm - b.height_mm) > 0.05:
        parts.append(f"size {a.width_mm:.1f}×{a.height_mm:.1f} → {b.width_mm:.1f}×{b.height_mm:.1f} mm")
    if a.stitch_count != b.stitch_count:
        parts.append(f"stitches {a.stitch_count:,} → {b.stitch_count:,}")
    for field, label in (("garment_style_name", "garment"), ("garment_color", "garment colour"), ("placement_name", "placement"),
                         ("placement_down_mm", "placement offset"), ("placement_across_mm", "placement offset"),
                         ("fabric_code", "fabric"), ("quantity", "quantity")):
        if getattr(a, field) != getattr(b, field):
            parts.append(f"{label} {getattr(a, field) or '—'} → {getattr(b, field) or '—'}")
    stops_a = [(s.thread_name, s.thread_code) for s in a.thread_stops]
    stops_b = [(s.thread_name, s.thread_code) for s in b.thread_stops]
    if stops_a != stops_b:
        parts.append("thread colours changed")
    return "; ".join(parts) if parts else "no measurable change"


def conditions_snapshot(v: ProofVersion, colorway_ordinal: int = 1, stops: Optional[list] = None) -> dict:
    stops = stops if stops is not None else list(v.thread_stops)
    return {
        "design_hash": v.design_hash, "version": v.version_number, "width_mm": v.width_mm, "height_mm": v.height_mm,
        "stitch_count": v.stitch_count, "garment_style_name": v.garment_style_name, "garment_color": v.garment_color,
        "garment_template": v.garment_template_id, "garment_zone": v.garment_zone,
        "placement_name": v.placement_name, "placement_notes": v.placement_notes, "quantity": v.quantity,
        "placement_offsets_mm": {"down": v.placement_down_mm, "across": v.placement_across_mm},
        "size_breakdown": v.size_breakdown, "fabric_code": v.fabric_code, "colorway_ordinal": colorway_ordinal,
        "thread_stops": [f"{s.stop_number}: {s.thread_name} {s.thread_code}".strip() for s in stops],
    }


# --- internal review (PRD: optional second set of eyes) ----------------------------------

def request_internal_review(db: Session, v: ProofVersion, user: User) -> None:
    proof = db.get(Proof, v.proof_id)
    if v.status != "ready_to_send" or proof.current_version_id != v.id:
        raise TransitionError("Only the current, unsent version can go for review.")
    seats = list(db.execute(select(AccountUser).where(AccountUser.account_id == proof.account_id, AccountUser.disabled_at.is_(None))).scalars())
    if len(seats) < 2:
        raise TransitionError("Internal review needs a second person on the account.")
    proof.status = "internal_review"
    proof.updated_at = utcnow()
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="internal_review_requested", actor_type="user", actor_id=user.id)
    account = db.get(Account, proof.account_id)
    for s in seats:
        if s.user_id != user.id and s.role in ("owner", "sales"):
            emailer.send(to_email=s.user.email, subject=f"Review requested: {proof.reference} {proof.title}",
                         text=f"{user.email} asked for a second look at version {v.version_number} before it goes to the customer.\n\n{config.PUBLIC_BASE_URL}/proofs/{proof.id}")


def pass_internal_review(db: Session, v: ProofVersion, user: User) -> None:
    proof = db.get(Proof, v.proof_id)
    if proof.status != "internal_review":
        raise TransitionError("This version isn't in review.")
    if v.composed_by == user.id:
        raise TransitionError("You composed this version; someone else has to review it.")
    v.reviewed_by = user.id
    proof.status = "ready_to_send"
    _refresh(db, proof)
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="internal_review_passed", actor_type="user", actor_id=user.id)


def fail_internal_review(db: Session, v: ProofVersion, user: User, *, note: str) -> None:
    proof = db.get(Proof, v.proof_id)
    if proof.status != "internal_review":
        raise TransitionError("This version isn't in review.")
    if v.composed_by == user.id:
        raise TransitionError("You composed this version; someone else has to review it.")
    if not note.strip():
        raise TransitionError("Say what needs changing.")
    v.review_note = note.strip()
    v.reviewed_by = user.id
    proof.status = "digitizing"
    proof.updated_at = utcnow()
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="internal_review_changes_requested", actor_type="user", actor_id=user.id, payload={"note": note.strip()})


def bounced_proofs(db: Session, account_id: str) -> list[Proof]:
    """Proofs whose most recent delivery attempt bounced (the in-app banner)."""
    out = []
    for p in db.execute(select(Proof).where(Proof.account_id == account_id, Proof.status.in_(("sent", "viewed", "awaiting_art")))).scalars():
        last = next((e for e in reversed(events.chain(db, p.id)) if e.event_type in ("delivered", "bounced")), None)
        if last is not None and last.event_type == "bounced":
            out.append(p)
    return out


def record_bounce(db: Session, *, email: str, reason: str) -> int:
    """A provider's bounce webhook: mark the newest sent proof to that address."""
    email = (email or "").strip().lower()
    n = 0
    for c in db.execute(select(Contact).where(Contact.email == email)).scalars():
        p = db.execute(select(Proof).where(Proof.contact_id == c.id, Proof.status.in_(("sent", "viewed", "awaiting_art"))).order_by(Proof.updated_at.desc())).scalars().first()
        if p is None:
            continue
        events.append(db, proof_id=p.id, proof_version_id=p.current_version_id, event_type="bounced", actor_type="system", payload={"to": email, "reason": reason})
        account = db.get(Account, p.account_id)
        if account.reply_to_email:
            emailer.send(to_email=account.reply_to_email, subject=f"Bounced: {p.reference} {p.title}", text=f"Email to {email} bounced ({reason}). Check the address and resend the link.")
        n += 1
    return n


# --- send ---------------------------------------------------------------------------

def send_version(db: Session, v: ProofVersion, user: Optional[User], *, base_url: str = "") -> str:
    """Sends version N: supersedes every earlier live version and revokes
    its tokens in the same transaction, mints the customer's link, emails
    it. Returns the public URL."""
    proof = db.get(Proof, v.proof_id)
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if proof.status in PROOF_TERMINAL:
        raise TransitionError("This proof is closed.")
    if proof.status == "internal_review":
        raise TransitionError("This version is in internal review; it can't be sent until the reviewer passes it.")
    if v.status != "ready_to_send":
        raise TransitionError(f"Version {v.version_number} is {v.status.replace('_', ' ')}; only a composed, unsent version can be sent.")
    if not v.terms_version_id:
        raise TransitionError("Set your approval terms before sending.")
    if not contact.email:
        raise TransitionError("The customer needs an email address.")
    ent = entitlements(db, account)
    first_send = not any(x.sent_at for x in proof.versions)
    if first_send and not ent.can_send:
        raise TransitionError("You've used your free proofs. Upgrade to PiperStitch Proofs to keep sending.")

    # Supersede: every earlier version in a customer-facing state.
    for old in proof.versions:
        if old.id != v.id and old.status not in ("superseded",) and old.sent_at:
            old.status = "superseded"
            old.superseded_by_id = v.id
            tokens.revoke_for_version(db, old.id)
            events.append(db, proof_id=proof.id, proof_version_id=old.id, event_type="superseded", actor_type="system",
                          payload={"by_version": v.version_number})

    plaintext, _t = tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose="proof",
                                proof_version_id=v.id, days=max(config.PROOF_TOKEN_DAYS, proof.response_window_days))
    v.status = "sent"
    v.sent_at = utcnow()
    v.response_expires_at = _iso(_now() + timedelta(days=proof.response_window_days))
    proof.current_version_id = v.id
    if first_send and not ent.proofs_enabled:
        ent.free_proofs_used += 1
    url = f"{base_url or config.PUBLIC_BASE_URL}/p/{plaintext}"
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="sent", actor_type="user" if user else "system",
                  actor_id=user.id if user else "", token_hash=_t.token_hash, payload={"to": contact.email, "expires_at": v.response_expires_at})
    _refresh(db, proof)

    shop = account.shop_name or "Your embroiderer"
    subject = f"{shop}: your embroidery proof {proof.reference} is ready"
    body = (f"Hi {contact.display_name or 'there'},\n\n{shop} has a proof ready for you: {proof.title}.\n\n"
            f"Open it here (no account needed):\n{url}\n\n"
            + (f"Note from the shop: {v.message_body}\n\n" if v.message_body else "")
            + f"Please approve or request changes by {v.response_expires_at[:10]}.\n\nThank you,\n{shop}")
    ok = emailer.send(to_email=contact.email, subject=subject, text=body, reply_to=account.reply_to_email)
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="delivered" if ok else "bounced", actor_type="system",
                  payload={"channel": "email", "to": contact.email})
    return url


# --- customer actions --------------------------------------------------------------

def record_view(db: Session, v: ProofVersion, *, token_hash: str, ip: str, user_agent: str) -> None:
    proof = db.get(Proof, v.proof_id)
    first = v.status == "sent"
    if first:
        v.status = "viewed"
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="opened" if first else "viewed", actor_type="contact",
                  actor_id=proof.contact_id, token_hash=token_hash, ip=ip, user_agent=user_agent)
    _refresh(db, proof)


def approve(db: Session, v: ProofVersion, *, signer_name: str, signer_email: str, notes: str, token_hash: str, ip: str,
            user_agent: str, local_offset: str = "", method: str = "self_service", on_behalf_channel: Optional[str] = None,
            on_behalf_evidence: str = "", recorded_by: Optional[User] = None, base_url: str = "", colorway_ordinal: int = 1) -> ApprovalRecord:
    proof = db.get(Proof, v.proof_id)
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if v.status not in ("sent", "viewed", "changes_requested"):
        raise TransitionError(f"This version can't be approved: it is {v.status.replace('_', ' ')}.")
    if not signer_name.strip():
        raise TransitionError("Please type your name to approve.")
    terms = db.get(TermsVersion, v.terms_version_id)
    if v.version_number != 1:
        # Every earlier version must already be superseded (invariant 2).
        for old in proof.versions:
            if old.id != v.id and old.status in VERSION_LIVE:
                raise TransitionError("An older version is still live; send this version first.")

    with_notes = bool(notes.strip())
    from . import colorways as _cw
    colorway = _cw.colorway_by_ordinal(v, colorway_ordinal) if colorway_ordinal > 1 else None
    if colorway_ordinal > 1 and colorway is None:
        raise TransitionError("That colorway doesn't exist on this version.")
    snapshot = conditions_snapshot(v, colorway_ordinal=colorway_ordinal, stops=_cw.stops_for(db, v, colorway_ordinal))
    v.status = "approved_with_notes" if with_notes else "approved"
    record = ApprovalRecord(
        proof_version_id=v.id, method=method, colorway_id=colorway.id if colorway else None, on_behalf_channel=on_behalf_channel, on_behalf_evidence=on_behalf_evidence,
        recorded_by_user_id=recorded_by.id if recorded_by else None, signer_name_typed=signer_name.strip(), signer_email=signer_email.strip().lower(),
        signer_ip=ip, signer_user_agent=user_agent, terms_version_id=terms.id if terms else None,
        consent_text_rendered=terms.consent_text if terms else texts.CONSENT_TEXT, terms_body_rendered=terms.body if terms else texts.DEFAULT_TERMS,
        notes=notes.strip(), conditions_snapshot_json=json.dumps(snapshot, sort_keys=True), artifact_hashes_json=v.artifact_hashes_json,
    )
    db.add(record)
    db.flush()
    e = events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="approved_with_notes" if with_notes else "approved",
                      actor_type="user" if method == "on_behalf" else "contact", actor_id=(recorded_by.id if recorded_by else proof.contact_id),
                      token_hash=token_hash, ip=ip, user_agent=user_agent, local_offset=local_offset,
                      payload={"approval_record_id": record.id, "signer_name": record.signer_name_typed, "method": method, "colorway": colorway_ordinal,
                               "on_behalf_channel": on_behalf_channel, "design_hash": v.design_hash, "artifact_hashes": v.artifact_hashes})
    record.event_chain_head = e.event_hash

    # The certificate: a first-class deliverable, hashed and (optionally) signed.
    cert = pdfgen.certificate_pdf(
        shop_name=account.shop_name or "Your embroiderer", reference=proof.reference, title=proof.title, version_number=v.version_number,
        approved_at=record.approved_at, method=method, signer_name=record.signer_name_typed, signer_email=record.signer_email,
        signer_ip=ip, signer_user_agent=user_agent, on_behalf=f"{on_behalf_channel or ''} {on_behalf_evidence or ''}".strip(),
        conditions=snapshot, artifact_hashes=v.artifact_hashes, consent_text=record.consent_text_rendered,
        terms_body=record.terms_body_rendered, events=[_event_dict(x) for x in events.chain(db, proof.id)], event_chain_head=e.event_hash,
        verify_url=f"{base_url or config.PUBLIC_BASE_URL}/verify", certificate_id=record.id,
    )
    record.certificate_sha256 = stitch.sha256(cert)
    record.certificate_signature = sign_hash(record.certificate_sha256)
    record.certificate_storage_key = storage.put(f"accounts/{account.id}/proofs/{proof.id}/v{v.version_number}/certificate-{record.id}.pdf", cert)
    plaintext, _t = tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose="certificate",
                                proof_version_id=v.id, approval_record_id=record.id, version_scope="pinned")
    _refresh(db, proof)

    shop = account.shop_name or "Your embroiderer"
    cert_url = f"{base_url or config.PUBLIC_BASE_URL}/c/{plaintext}"
    to = record.signer_email or contact.email
    if to:
        emailer.send(to_email=to, subject=f"{shop}: approval recorded for {proof.reference}",
                     text=(f"Thank you. Your approval of {proof.title} (version {v.version_number}) was recorded {record.approved_at}.\n\n"
                           f"Your Certificate of Approval, with the exact approved file hashes:\n{cert_url}\n\n"
                           f"This link does not expire. Certificate SHA-256: {record.certificate_sha256}\n\n{shop}"),
                     reply_to=account.reply_to_email)
    if account.reply_to_email:
        emailer.send(to_email=account.reply_to_email, subject=f"Approved: {proof.reference} {proof.title} (v{v.version_number})",
                     text=f"{record.signer_name_typed} approved version {v.version_number}{' with notes: ' + record.notes if record.notes else ''}.\n\nRelease it to production from the pipeline board.")
    return record


def _event_dict(e) -> dict:
    return {"sequence": e.sequence, "occurred_at": e.occurred_at, "event_type": e.event_type, "actor_type": e.actor_type,
            "event_hash": e.event_hash, "prev_event_hash": e.prev_event_hash, "payload": e.payload}


def request_changes(db: Session, v: ProofVersion, *, items: list[dict], token_hash: str, ip: str, user_agent: str) -> list[ChangeRequest]:
    proof = db.get(Proof, v.proof_id)
    if v.status not in ("sent", "viewed", "changes_requested"):
        raise TransitionError(f"This version can't take changes: it is {v.status.replace('_', ' ')}.")
    items = [i for i in items if (i.get("body") or "").strip() or i.get("chip_code")]
    if not items:
        raise TransitionError("Tell the shop what to change -- at least one note.")
    existing = list(db.execute(select(ChangeRequest).where(ChangeRequest.proof_version_id == v.id)).scalars())
    out = []
    for i, item in enumerate(items, start=len(existing) + 1):
        cr = ChangeRequest(proof_version_id=v.id, sequence=i, kind=item.get("kind") or ("chip" if item.get("chip_code") else "freetext"),
                           chip_code=item.get("chip_code") or "", body=(item.get("body") or "").strip(), pin_x=item.get("pin_x"), pin_y=item.get("pin_y"))
        db.add(cr)
        out.append(cr)
    db.flush()
    v.status = "changes_requested"
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="changes_requested", actor_type="contact", actor_id=proof.contact_id,
                  token_hash=token_hash, ip=ip, user_agent=user_agent, payload={"items": [{"kind": c.kind, "chip": c.chip_code, "body": c.body, "pin": [c.pin_x, c.pin_y]} for c in out]})
    _refresh(db, proof)
    account = db.get(Account, proof.account_id)
    if account.reply_to_email:
        emailer.send(to_email=account.reply_to_email, subject=f"Changes requested: {proof.reference} {proof.title}",
                     text="The customer asked for changes:\n\n" + "\n".join(f"- {c.body or c.chip_code}" for c in out))
    return out


def ask_question(db: Session, v: ProofVersion, *, body: str, token_hash: str, ip: str, user_agent: str) -> Message:
    proof = db.get(Proof, v.proof_id)
    if not body.strip():
        raise TransitionError("Type a question first.")
    m = Message(proof_id=proof.id, proof_version_id=v.id, direction="inbound", author_type="contact", author_id=proof.contact_id, body=body.strip())
    db.add(m)
    db.flush()
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="question_asked", actor_type="contact", actor_id=proof.contact_id,
                  token_hash=token_hash, ip=ip, user_agent=user_agent, payload={"message_id": m.id})
    account = db.get(Account, proof.account_id)
    if account.reply_to_email:
        emailer.send(to_email=account.reply_to_email, subject=f"Question on {proof.reference} {proof.title}", text=body.strip())
    return m


def answer_question(db: Session, proof: Proof, user: User, *, body: str, base_url: str = "") -> Message:
    if not body.strip():
        raise TransitionError("Type a reply first.")
    m = Message(proof_id=proof.id, proof_version_id=proof.current_version_id, direction="outbound", author_type="user", author_id=user.id, body=body.strip())
    db.add(m)
    db.flush()
    events.append(db, proof_id=proof.id, proof_version_id=proof.current_version_id, event_type="question_answered", actor_type="user", actor_id=user.id,
                  payload={"message_id": m.id})
    contact = db.get(Contact, proof.contact_id)
    account = db.get(Account, proof.account_id)
    link = live_link(db, proof)
    if contact.email:
        emailer.send(to_email=contact.email, subject=f"{account.shop_name or 'Your embroiderer'} replied about {proof.title}",
                     text=body.strip() + (f"\n\nView the proof: {base_url or config.PUBLIC_BASE_URL}/p/{link}" if link else ""), reply_to=account.reply_to_email)
    return m


def live_link(db: Session, proof: Proof) -> Optional[str]:
    """No plaintext is stored, so a live link can't be recovered -- only re-minted. Returns None here; routes re-mint on 'resend'."""
    return None


def decline(db: Session, v: ProofVersion, *, reason: str, token_hash: str, ip: str, user_agent: str) -> None:
    proof = db.get(Proof, v.proof_id)
    if v.status not in ("sent", "viewed", "changes_requested"):
        raise TransitionError("This version can't be declined now.")
    v.status = "declined"
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="declined", actor_type="contact", actor_id=proof.contact_id,
                  token_hash=token_hash, ip=ip, user_agent=user_agent, payload={"reason": reason.strip()})
    _refresh(db, proof)


# --- shop actions -----------------------------------------------------------------------

def resend(db: Session, v: ProofVersion, user: Optional[User], *, base_url: str = "") -> str:
    """A fresh link for the current version (old ones revoked): for an
    expired or declined version, or when the customer lost the email."""
    proof = db.get(Proof, v.proof_id)
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if v.status not in ("sent", "viewed", "changes_requested", "expired", "declined"):
        raise TransitionError("Only a sent version can be resent.")
    tokens.revoke_for_version(db, v.id)
    plaintext, t = tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose="proof", proof_version_id=v.id)
    if v.status in ("expired", "declined"):
        v.status = "sent"
        v.response_expires_at = _iso(_now() + timedelta(days=proof.response_window_days))
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="resent", actor_type="user" if user else "system",
                  actor_id=user.id if user else "", token_hash=t.token_hash)
    _refresh(db, proof)
    url = f"{base_url or config.PUBLIC_BASE_URL}/p/{plaintext}"
    shop = account.shop_name or "Your embroiderer"
    emailer.send(to_email=contact.email, subject=f"{shop}: your proof {proof.reference} — new link",
                 text=f"Here is a fresh link to your proof {proof.title}:\n{url}\n\n{shop}", reply_to=account.reply_to_email)
    return url


def verify_artifacts(db: Session, v: ProofVersion) -> tuple[bool, list[str]]:
    """Re-hash every stored artifact against the version's recorded hashes."""
    proof = db.get(Proof, v.proof_id)
    expected = v.artifact_hashes
    problems = []
    checks = {"pdf": "proof.pdf", "render": "render.png", "hero": "hero.png", "social": "social.png", "mockup": "mockup.png", "diagram": "diagram.png"}
    for key, name in checks.items():
        if not expected.get(key):
            continue   # optional artifact not produced for this version
        k = _artifact_key(proof, v.version_number, name)
        if not storage.exists(k) or stitch.sha256(storage.get(k)) != expected.get(key):
            problems.append(key)
    for fmt, h in (expected.get("machine_files") or {}).items():
        k = _artifact_key(proof, v.version_number, f"design.{fmt}")
        if not storage.exists(k) or stitch.sha256(storage.get(k)) != h:
            problems.append(f"machine_files.{fmt}")
    for ordinal, entry in (expected.get("colorways") or {}).items():
        for name, h in entry.items():
            k = _artifact_key(proof, v.version_number, f"cw{ordinal}-{name}.png")
            if not storage.exists(k) or stitch.sha256(storage.get(k)) != h:
                problems.append(f"colorways.{ordinal}.{name}")
    return (not problems), problems


def release(db: Session, proof: Proof, user: User) -> None:
    v = db.get(ProofVersion, proof.current_version_id) if proof.current_version_id else None
    if v is None or v.status not in ("approved", "approved_with_notes"):
        raise TransitionError("Release needs an approved version.")
    ok, problems = verify_artifacts(db, v)
    if not ok:
        raise TransitionError("Artifact hashes don't verify (" + ", ".join(problems) + "); the approved files have changed. Do not release.")
    proof.status = "released"
    proof.released_at = utcnow()
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="released", actor_type="user", actor_id=user.id,
                  payload={"artifact_hashes": v.artifact_hashes})
    proof.updated_at = utcnow()


def complete(db: Session, proof: Proof, user: User) -> None:
    if proof.status != "released":
        raise TransitionError("Only a released job can be marked sewn.")
    proof.status = "completed"
    proof.completed_at = utcnow()
    events.append(db, proof_id=proof.id, proof_version_id=proof.current_version_id, event_type="completed", actor_type="user", actor_id=user.id)


def void(db: Session, proof: Proof, user: User, *, reason: str) -> None:
    if proof.status in PROOF_TERMINAL:
        raise TransitionError("This proof is already closed.")
    proof.status = "void"
    proof.void_reason = reason.strip()
    tokens.revoke_for_proof(db, proof.id)
    events.append(db, proof_id=proof.id, proof_version_id=proof.current_version_id, event_type="voided", actor_type="user", actor_id=user.id,
                  payload={"reason": reason.strip()})
    contact = db.get(Contact, proof.contact_id)
    account = db.get(Account, proof.account_id)
    if contact.email and proof.current_version_id:
        emailer.send(to_email=contact.email, subject=f"{account.shop_name or 'Your embroiderer'}: proof {proof.reference} cancelled",
                     text=f"The proof for {proof.title} has been cancelled by the shop.{(' Reason: ' + reason.strip()) if reason.strip() else ''}", reply_to=account.reply_to_email)


def expire_due(db: Session) -> int:
    """Scheduler: versions past their response window with no answer."""
    n = 0
    now = _now()
    for v in db.execute(select(ProofVersion).where(ProofVersion.status.in_(("sent", "viewed")))).scalars():
        if v.response_expires_at and parse_ts(v.response_expires_at) < now:
            v.status = "expired"
            events.append(db, proof_id=v.proof_id, proof_version_id=v.id, event_type="expired", actor_type="system")
            _refresh(db, db.get(Proof, v.proof_id))
            n += 1
    return n


# --- certificates ------------------------------------------------------------------------

def _signing_key():
    if not config.CERTIFICATE_SIGNING_KEY:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.urlsafe_b64decode(config.CERTIFICATE_SIGNING_KEY + "=" * (-len(config.CERTIFICATE_SIGNING_KEY) % 4))
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_hash(sha256_hex: str) -> str:
    key = _signing_key()
    if key is None:
        return ""
    return base64.urlsafe_b64encode(key.sign(sha256_hex.encode())).decode().rstrip("=")


def verify_certificate(db: Session, sha256_hex: str) -> dict:
    """GET /verify/{sha} -- unauthenticated, content-addressed. Pass only
    when the certificate is known, its stored bytes still hash to it, the
    proof's stored artifacts still hash to the certificate's recorded
    values, and the event chain verifies."""
    sha = (sha256_hex or "").strip().lower()
    record = db.execute(select(ApprovalRecord).where(ApprovalRecord.certificate_sha256 == sha)).scalar_one_or_none()
    if record is None:
        return {"result": "fail", "reason": "unknown certificate"}
    v = db.get(ProofVersion, record.proof_version_id)
    proof = db.get(Proof, v.proof_id)
    problems = []
    if not storage.exists(record.certificate_storage_key) or stitch.sha256(storage.get(record.certificate_storage_key)) != sha:
        problems.append("certificate bytes changed")
    ok, artifact_problems = verify_artifacts(db, v)
    if not ok:
        problems += [f"artifact changed: {p}" for p in artifact_problems]
    if record.artifact_hashes != v.artifact_hashes:
        problems.append("recorded hashes differ from the version's")
    chain_ok, chain_msg = events.verify_chain(db, proof.id)
    if not chain_ok:
        problems.append(f"event chain: {chain_msg}")
    return {
        "result": "pass" if not problems else "fail", "reasons": problems, "certificate_sha256": sha,
        "reference": proof.reference, "version": v.version_number, "approved_at": record.approved_at, "method": record.method,
        "design_hash": v.design_hash, "artifact_hashes": record.artifact_hashes, "event_chain_head": record.event_chain_head,
        "signature": record.certificate_signature or None,
    }
