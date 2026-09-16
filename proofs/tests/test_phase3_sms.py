"""SMS/MMS through Twilio: consent gating, STOP/START, the link by text,
SMS reminder steps, MMS artwork intake, webhook signature."""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as database, proofs, reminders, sms
from app.db import AccountUser, Contact, Proof, TriageReport, User
from app.main import app
from tests.test_phase1 import create_and_compose, send_current, sign_in
from tests.test_phase2_intake import business_card_photo
from tests.test_phase2_reminders import _noon_for, _set_sent_ago


def _texts():
    p = Path(os.environ["SMS_OUTBOX_DIR"])
    p.mkdir(parents=True, exist_ok=True)
    return [json.loads(f.read_text()) for f in sorted(p.glob("*.json"))]


def _clear():
    for f in Path(os.environ["SMS_OUTBOX_DIR"]).glob("*.json"):
        f.unlink()


def _enable_sms(email):
    with database.SessionLocal() as db:
        m = db.execute(select(AccountUser).join(User).where(User.email == email)).scalar_one()
        proofs.entitlements(db, m.account).sms_enabled = True
        db.commit()


def test_link_is_texted_only_with_consent_and_stop_opts_out(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-sms@shop.example", outbox)
    _enable_sms("dana-sms@shop.example")
    _clear()
    pid = create_and_compose(shop, document, email="tx@example.com")
    with database.SessionLocal() as db:
        c = db.get(Contact, db.get(Proof, pid).contact_id)
        c.phone = "(941) 555-0199"       # no opt-in yet
        db.commit()
    send_current(shop, pid, outbox, "tx@example.com")
    assert _texts() == [], "no consent, no text"
    with database.SessionLocal() as db:
        c = db.get(Contact, db.get(Proof, pid).contact_id)
        c.sms_opt_in_at, c.sms_opt_in_text = "2026-09-16T00:00:00Z", "ticked the box"
        db.commit()
        v = db.get(database.ProofVersion, db.get(Proof, pid).current_version_id)
        vid = v.id
    # Resend is email-only; the SMS goes with sends and reminders. Open the
    # proof (so the 48 h email step is skipped) and trigger the 72 h SMS step.
    r = shop.post(f"/proofs/{pid}/versions/{vid}/resend", follow_redirects=False)
    link = re.search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", outbox.latest_to("tx@example.com")["text"]).group(1)
    TestClient(app).get(link)
    _set_sent_ago(pid, 80)
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for())
        db.commit()
    texts = _texts()
    assert len(texts) == 1 and texts[0]["to"] == "+19415550199" and "/p/" in texts[0]["body"] and "STOP" in texts[0]["body"]
    assert len(texts[0]["body"]) < 320
    # STOP from that number: opted out; the next SMS step is suppressed.
    hook = TestClient(app)
    r = hook.post("/webhooks/twilio/inbound", data={"From": "+19415550199", "Body": "STOP", "NumMedia": "0"})
    assert r.status_code == 200 and "unsubscribed" in r.text and r.headers["content-type"].startswith("application/xml")
    with database.SessionLocal() as db:
        c = db.get(Contact, db.get(Proof, pid).contact_id)
        assert c.opted_out_at
        assert sms.can_text(db, db.get(database.Account, c.account_id), c) == (False, "opted_out")
    r = hook.post("/webhooks/twilio/inbound", data={"From": "9415550199", "Body": "start", "NumMedia": "0"})
    assert "opted in" in r.text
    with database.SessionLocal() as db:
        assert db.get(Contact, db.get(Proof, pid).contact_id).opted_out_at is None


def test_mms_photo_becomes_artwork_on_the_customers_open_proof(outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-mms@shop.example", outbox)
    _enable_sms("dana-mms@shop.example")
    r = shop.post("/proofs/new", data={"title": "Van logo", "contact_name": "Pat", "contact_email": "pat-mms@example.com", "contact_phone": "941-555-0123"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    shop.post(f"/proofs/{pid}/intake/send", follow_redirects=False)
    photo = business_card_photo(1600, 1200)
    with database.SessionLocal() as db:
        reply = sms.inbound(db, {"From": "+19415550123", "Body": "here's the logo", "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/media/1", "MediaContentType0": "image/jpeg"},
                            fetch_media=lambda url: photo)
        db.commit()
        assert reply.startswith("Got it -- 1 file")
        p = db.get(Proof, pid)
        assert p.status == "art_received"
        rep = db.execute(select(TriageReport).where(TriageReport.proof_id == pid)).scalar_one()
        assert rep.file.source == "mms" and rep.file.original_filename == "mms-1.jpg" and json.loads(rep.report_json)["pixel_width"] == 1600
    # Unknown number with media: told to email instead; nothing stored.
    with database.SessionLocal() as db:
        reply = sms.inbound(db, {"From": "+10000000000", "Body": "", "NumMedia": "1", "MediaUrl0": "x", "MediaContentType0": "image/jpeg"}, fetch_media=lambda url: photo)
        assert "don't recognise" in reply


def test_twilio_signature_is_checked_when_a_token_is_set(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "12345")
    url = "https://proofs.example/webhooks/twilio/inbound"
    form = {"From": "+15550001111", "Body": "hi"}
    import base64, hashlib, hmac
    good = base64.b64encode(hmac.new(b"12345", (url + "Bodyhi" + "From+15550001111").encode(), hashlib.sha1).digest()).decode()
    assert sms.verify_signature(url, form, good)
    assert not sms.verify_signature(url, form, "nope")
    assert sms.normalize("(941) 555-0100") == "+19415550100" and sms.normalize("+44 20 7946 0958") == "+44 20 7946 0958"
