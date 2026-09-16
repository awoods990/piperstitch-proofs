"""Email ingest (PRD 5.1): DKIM/DMARC enforcement, trusted senders,
quarantine for unknown senders, forwarded mail, rate limits."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as database, ingest
from app.db import Account, AccountUser, InboundEmail, Proof, TriageReport, User
from app.main import app
from tests.test_phase1 import sign_in
from tests.test_phase2_intake import business_card_photo, tiny_logo


def _msg(to, sender, subject="Logo for polos", body="Here's our logo.", attachments=None, auth="dkim=pass header.d=example.com; spf=pass; dmarc=pass", name="Marcus Lee"):
    headers = [{"Name": "Authentication-Results", "Value": auth}] if auth else []
    return {"From": sender, "FromFull": {"Email": sender, "Name": name}, "To": to, "OriginalRecipient": to, "Subject": subject, "TextBody": body,
            "Headers": headers, "MessageID": "m-" + subject, "Attachments": [{"Name": n, "Content": base64.b64encode(d).decode(), "ContentType": ct, "ContentLength": len(d)} for n, d, ct in (attachments or [])]}


def _setup(outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-ingest@shop.example", outbox)
    shop.post("/settings", data={"shop_name": "Sandpiper Stitchworks", "reply_to_email": "dana-ingest@shop.example", "release_gate_policy": "soft", "default_response_window_days": "7",
                                 "terms_body": "", "consent_text": ""}, follow_redirects=False)
    shop.post("/settings/art-address", follow_redirects=False)
    with database.SessionLocal() as db:
        m = db.execute(select(AccountUser).join(User).where(User.email == "dana-ingest@shop.example")).scalar_one()
        slug = m.account.slug
    return shop, slug


def test_dkim_failure_is_rejected_and_unknown_senders_are_quarantined(outbox):
    shop, slug = _setup(outbox)
    to = f"art@{slug}.piperstitch.com"
    hook = TestClient(app)
    r = hook.post("/webhooks/postmark/inbound", json=_msg(to, "spoof@evil.example", auth="dkim=fail; dmarc=fail"))
    assert r.json()["status"] == "rejected" and "authentication" in r.json()["reason"]
    r = hook.post("/webhooks/postmark/inbound", json=_msg(to, "stranger@somewhere.example", attachments=[("logo.png", business_card_photo(1200, 900), "image/png")]))
    assert r.json()["status"] == "quarantined" and r.json()["proof_id"] is None
    assert "waiting to be accepted" in shop.get("/proofs").text
    with database.SessionLocal() as db:
        row = db.execute(select(InboundEmail).where(InboundEmail.from_email == "stranger@somewhere.example")).scalar_one()
        assert row.attachments_json != "[]"
    # Unknown address: rejected outright.
    r = hook.post("/webhooks/postmark/inbound", json=_msg("art@nobody-here.piperstitch.com", "x@y.example"))
    assert r.json()["status"] == "rejected"
    # The shop accepts the quarantined one -> proof in art_received, triaged, contact created.
    r = shop.post(f"/inbound/{row.id}/accept", follow_redirects=False)
    with database.SessionLocal() as db:
        row = db.get(InboundEmail, row.id)
        assert row.status == "accepted" and row.proof_id
        p = db.get(Proof, row.proof_id)
        assert p.status == "art_received" and p.contact.email == "stranger@somewhere.example" and p.title == "Logo for polos"
        assert db.execute(select(TriageReport).where(TriageReport.proof_id == p.id)).scalar_one()


def test_forwarded_mail_from_the_shop_creates_a_proof_with_the_customer_from_the_body(outbox):
    shop, slug = _setup(outbox)
    to = f"art+{slug}@inbound.piperstitch.com"
    body = "FYI\n\n---------- Forwarded message ---------\nFrom: Pat Customer <pat@gulfcoast.example>\nDate: Mon\nSubject: our logo\n\nHi Dana, here's the logo. 24 polos please.\n-- \nPat"
    hook = TestClient(app)
    r = hook.post("/webhooks/postmark/inbound", json=_msg(to, "dana-ingest@shop.example", subject="Fwd: our logo", body=body, name="Dana",
                                                          attachments=[("logo.png", business_card_photo(1000, 800), "image/png"), ("sig.png", tiny_logo(), "image/png")]))
    assert r.json()["status"] == "accepted", r.json()
    with database.SessionLocal() as db:
        p = db.get(Proof, r.json()["proof_id"])
        assert p.title == "our logo" and p.contact.email == "pat@gulfcoast.example" and p.contact.display_name == "Pat Customer"
        reports = list(db.execute(select(TriageReport).where(TriageReport.proof_id == p.id)).scalars())
        assert [x.file.original_filename for x in reports] == ["logo.png"], "the signature-sized image is filtered out"
        from app.db import Message
        msgs = list(db.execute(select(Message).where(Message.proof_id == p.id)).scalars())
        assert msgs and "24 polos" in msgs[0].body


def test_rate_limit_holds_overflow(outbox):
    shop, slug = _setup(outbox)
    to = f"art@{slug}.piperstitch.com"
    hook = TestClient(app)
    statuses = [hook.post("/webhooks/postmark/inbound", json=_msg(to, "flood@x.example", subject=f"m{i}")).json()["status"] for i in range(12)]
    assert statuses[:10] == ["quarantined"] * 10 and statuses[10:] == ["rate_limited"] * 2
    with database.SessionLocal() as db:
        a = db.execute(select(Account).where(Account.slug == slug)).scalar_one()
        # Near-miss slugs are refused.
        assert ingest._slug_taken(db, slug.replace("-", "")) and ingest._slug_taken(db, slug + "-2")
