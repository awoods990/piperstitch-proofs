"""Phase 2 acceptance criteria (intake links, the Readiness Report,
blocker gating with override, the shareable report)."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import db as database, events, intake, triage
from app.db import Proof, TriageFinding, TriageReport
from app.main import app
from tests.test_phase1 import sign_in


def business_card_photo(width=4000, height=3000) -> bytes:
    """A 12 MP 'phone photo' of a business card: white card, a bold logo,
    and a tagline in thin 20 px letters."""
    img = Image.new("RGB", (width, height), (245, 245, 243))
    d = ImageDraw.Draw(img)
    d.rectangle([600, 500, 3400, 2500], fill=(255, 255, 255))
    d.ellipse([900, 800, 1700, 1600], fill=(20, 60, 140))
    d.rectangle([1900, 900, 3000, 1300], fill=(180, 30, 40))
    d.rectangle([1900, 1450, 3000, 1500], fill=(20, 60, 140))
    # Tagline: thin strokes that will be tiny at a left-chest size.
    for i in range(12):
        x = 1900 + i * 90
        d.rectangle([x, 1700, x + 12, 1900], fill=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def tiny_logo() -> bytes:
    img = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([10, 10, 290, 110], fill=(20, 60, 140, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_triage_measures_a_phone_photo_of_a_business_card():
    photo = business_card_photo()
    r = triage.analyze(photo, "IMG_4021.jpg", requested_width_mm=89, needle_count=6)   # 3.5 in
    assert r.kind == "raster" and r.pixel_width == 4000
    assert r.effective_ppi == 1142   # 4000 px / 3.5 in
    assert r.white_box, "the card's white background should be detected"
    assert r.color_count >= 3
    codes = {f.code: f for f in r.findings}
    assert "resolution_ok" in codes and "white_box" in codes and "color_count" in codes
    assert r.min_feature_mm is not None and r.min_feature_mm < 1.0, r.min_feature_mm
    assert "detail_too_fine" in codes and "mm" in codes["detail_too_fine"].message
    assert not r.blockers
    # Same file, 4 in wide, on a cap: the height check fires.
    r2 = triage.analyze(photo, "IMG_4021.jpg", requested_width_mm=102, is_cap=True)
    assert any(f.code == "cap_height" for f in r2.findings)


def test_triage_blocks_low_resolution_and_unsupported_files():
    r = triage.analyze(tiny_logo(), "logo.png", requested_width_mm=254)   # 300 px at 10 in = 30 PPI
    assert r.effective_ppi == 30 and r.blockers and r.blockers[0].code == "low_resolution"
    assert r.has_transparency and not r.white_box
    r = triage.analyze(b"8BPS\x00\x01" + bytes(100), "logo.psd", requested_width_mm=100)
    assert r.kind == "unknown" and r.blockers[0].code == "unsupported_format"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><text>Hi</text><rect fill="#ff0000"/><rect fill="#0000ff"/></svg>'
    r = triage.analyze(svg, "logo.svg", requested_width_mm=100)
    assert r.is_vector and r.color_count == 2 and {f.code for f in r.findings} >= {"svg_live_text", "gradient"}
    r = triage.analyze(Path(__file__).parent.joinpath("fixtures/cap.dst").read_bytes(), "cap.dst", requested_width_mm=100)
    assert r.kind == "machine"


def test_intake_link_collects_artwork_and_answers_then_gates_composition(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-intake@shop.example", outbox)
    r = shop.post("/proofs/new", data={"title": "Cap logo", "contact_name": "Marcus", "contact_email": "marcus-intake@example.com"}, follow_redirects=False)
    proof_id = r.headers["location"].rsplit("/", 1)[1]
    r = shop.post(f"/proofs/{proof_id}/intake/send", data={"message": "Send the biggest file you have."}, follow_redirects=False)
    assert r.status_code == 303
    mail = outbox.latest_to("marcus-intake@example.com")
    url = re.search(r"(http://testserver/i/[A-Za-z0-9_\-]+)", mail["text"]).group(1)
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.status == "awaiting_art" and p.intake_expires_at
    customer = TestClient(app)
    page = customer.get(url)
    assert page.status_code == 200 and "Send us your artwork" in page.text and "How wide should the logo be" in page.text
    # A low-resolution file at the requested size -> blocker.
    r = customer.post(url, data={"garment_style_name": "structured caps", "garment_color": "black", "quantity": "48", "width_in": "10",
                                 "placement_name": "cap front", "due_date": "2026-10-01", "notes": "Rush if possible", "phone": "941-555-0100", "sms_consent": "yes"},
                      files=[("files", ("logo.png", tiny_logo(), "image/png"))])
    assert r.status_code == 200 and "we have your artwork" in r.text
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.status == "art_received" and p.requested_width_mm == 254.0 and p.due_date == "2026-10-01"
        assert p.contact.phone == "941-555-0100" and p.contact.sms_opt_in_at and "STOP" in p.contact.sms_opt_in_text
        reports = list(db.execute(database.select(TriageReport).where(TriageReport.proof_id == proof_id)).scalars())
        assert len(reports) == 1 and reports[0].file.sha256 and reports[0].file.scan_status == "skipped"
        blockers = intake.open_blockers(db, p)
        assert len(blockers) == 1 and blockers[0].code == "low_resolution"
        types = [e.event_type for e in events.chain(db, proof_id)]
        assert types[-3:] == ["art_uploaded", "triage_completed"] or "triage_completed" in types
        assert events.verify_chain(db, proof_id)[0]
    # The link is single-use.
    assert "already received" in customer.get(url).text
    # The shop sees the report and cannot compose past the blocker.
    detail = shop.get(f"/proofs/{proof_id}").text
    assert "Art Readiness Report" in detail and "Resolution is low" in detail and "Composing is blocked" in detail and "Rush if possible" in detail
    r = shop.post(f"/proofs/{proof_id}/compose", data={"quantity": "48"}, files={"document_file": ("cap.stitchpilot", json.dumps(document), "application/json")}, follow_redirects=False)
    assert r.headers["location"] == f"/proofs/{proof_id}"
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).current_version_id is None
    # Override with a reason -> composing works and the override is on the record.
    r = shop.post(f"/proofs/{proof_id}/triage/override", data={"reason": "customer accepts a traced logo at this size"}, follow_redirects=False)
    r = shop.post(f"/proofs/{proof_id}/compose", data={"quantity": "48", "garment_template_id": "structured_cap", "garment_zone": "front", "garment_color": "Black"},
                  files={"document_file": ("cap.stitchpilot", json.dumps(document), "application/json")}, follow_redirects=False)
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.current_version_id is not None and p.status == "ready_to_send"
        assert p.triage_override_reason.startswith("customer accepts")
        assert not intake.open_blockers(db, p)
        types = [e.event_type for e in events.chain(db, proof_id)]
        assert "triage_overridden" in types and types.index("triage_overridden") < types.index("composed")
    # Share the report with the customer: a read-only link.
    r = shop.post(f"/proofs/{proof_id}/triage/share", data={"message": "The file is small; we traced it."}, follow_redirects=False)
    rurl = re.search(r"(http://testserver/r/[A-Za-z0-9_\-]+)", outbox.latest_to("marcus-intake@example.com")["text"]).group(1)
    page = customer.get(rurl)
    assert page.status_code == 200 and "What we found" in page.text and "Resolution is low" in page.text and "we traced it" in page.text
    assert "Approve" not in page.text


def test_shop_direct_upload_triages_and_intake_expiry_sweep(outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-upload@shop.example", outbox)
    r = shop.post("/proofs/new", data={"title": "Polo logo", "contact_name": "Pat", "contact_email": "pat@example.com"}, follow_redirects=False)
    proof_id = r.headers["location"].rsplit("/", 1)[1]
    r = shop.post(f"/proofs/{proof_id}/intake/upload", data={"requested_width": "3.5", "width_units": "in", "is_cap": "no"},
                  files=[("files", ("card.jpg", business_card_photo(2000, 1500), "image/jpeg"))], follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.status == "art_received" and abs(p.requested_width_mm - 88.9) < 0.1
        rep = db.execute(database.select(TriageReport).where(TriageReport.proof_id == proof_id)).scalar_one()
        assert rep.effective_ppi == 571 and not intake.open_blockers(db, p)
    # Expiry sweep on an awaiting_art proof.
    r = shop.post("/proofs/new", data={"title": "Old", "contact_name": "Q", "contact_email": "q@example.com"}, follow_redirects=False)
    pid2 = r.headers["location"].rsplit("/", 1)[1]
    shop.post(f"/proofs/{pid2}/intake/send", follow_redirects=False)
    with database.SessionLocal() as db:
        p2 = db.get(Proof, pid2)
        p2.intake_expires_at = "2000-01-01T00:00:00Z"
        db.commit()
        assert intake.expire_intakes(db) == 1
        db.commit()
        assert db.get(Proof, pid2).status == "intake_expired"
    assert "expired" in shop.get(f"/proofs/{pid2}").text
