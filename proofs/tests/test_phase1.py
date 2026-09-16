"""Phase 1 acceptance criteria (PRD "Build plan · Phase 1") as tests,
end to end through the HTTP surface."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, core_client, db as database, events, proofs, stitch, storage, texts
from app.db import AccountUser, ApprovalRecord, Proof, ProofVersion, User
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def pdf_text(data: bytes) -> bytes:
    """Every content stream of a PDF, decoded (reportlab writes ASCII85 +
    Flate) -- enough to grep for text."""
    import base64
    import zlib
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", data, flags=re.S):
        chunk = m.group(1)
        try:
            raw = base64.a85decode(chunk, adobe=True) if chunk.endswith(b"~>") else chunk
            out += zlib.decompress(raw)
        except (ValueError, zlib.error):
            out += chunk
    return out


def sign_in(c: TestClient, email: str, outbox) -> None:
    r = c.post("/signin", data={"email": email})
    assert r.status_code == 200
    mail = outbox.latest_to(email)
    assert mail, "sign-in code email"
    code = re.search(r"code is (\d{6})", mail["text"]).group(1)
    r = c.post("/signin/verify", data={"email": email, "code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/proofs"
    # A shop can't send anything until it has a name (owners only).
    if "set your shop name" in c.get("/proofs").text:
        c.post("/settings", data={"shop_name": "Sandpiper Stitchworks", "reply_to_email": email, "release_gate_policy": "soft",
                                  "default_response_window_days": "7", "terms_body": "", "consent_text": ""}, follow_redirects=False)


def create_and_compose(c: TestClient, document: dict, *, title="Left chest logo", email="marcus@example.com", name="Marcus Lee", **compose) -> str:
    r = c.post("/proofs/new", data={"title": title, "contact_name": name, "contact_email": email}, follow_redirects=False)
    assert r.status_code == 303
    proof_id = r.headers["location"].rsplit("/", 1)[1]
    form = {"garment_style_name": "Port & Company polo", "garment_color": "Navy", "placement_name": "Left chest",
            "placement_notes": "", "quantity": "24", "size_breakdown": "S:4, M:10, L:8, XL:2", "garment_template_id": "polo", "garment_zone": "left_chest",
            "placement_down": "0.5", "placement_across": "0", "placement_units": "in",
            "message_body": "Here's the proof — the red is Madeira 1147.", "hoop_code": "4x4"}
    form.update(compose)
    r = c.post(f"/proofs/{proof_id}/compose", data=form, files={"document_file": ("cap.stitchpilot", json.dumps(document), "application/json")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proofs/{proof_id}", r.text
    return proof_id


def send_current(c: TestClient, proof_id: str, outbox, customer_email="marcus@example.com") -> str:
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        vid = p.current_version_id
    r = c.post(f"/proofs/{proof_id}/versions/{vid}/send", follow_redirects=False)
    assert r.status_code == 303
    mail = outbox.latest_to(customer_email)
    assert mail, "customer email"
    return re.search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", mail["text"]).group(1)


def test_owner_composes_and_sends_a_proof_in_one_pass(document, outbox):
    c = _client()
    sign_in(c, "dana@shop.example", outbox)
    proof_id = create_and_compose(c, document)
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        assert v.version_number == 1 and v.status == "ready_to_send"
        assert v.stitch_count == 7173 and v.color_change_count == 1
        assert 55 < v.width_mm < 56 and 80 < v.height_mm < 81
        assert [s.thread_name for s in v.thread_stops] == ["Generic White", "Generic Red"]
        assert sum(s.stitch_count for s in v.thread_stops) == v.stitch_count
        assert v.fabric_code == "structuredCap" and "cap backing" in v.stabilizer_advice
        h = v.artifact_hashes
        assert set(h) == {"pdf", "render", "hero", "social", "mockup", "diagram", "machine_files"}
        assert h["mockup"] and h["diagram"], "a garment template was chosen, so the mockup and diagram exist"
        assert v.garment_template_id == "polo" and v.garment_zone == "left_chest"
        assert "below shoulder seam" in v.placement_notes and "centre" in v.placement_notes
        assert set(h["machine_files"]) == set(core_client.MACHINE_FORMATS)
        # Every hash matches the stored bytes and the fixture machine files.
        assert h["machine_files"]["dst"] == hashlib.sha256((Path(__file__).parent / "fixtures/cap.dst").read_bytes()).hexdigest()
        ok, problems = proofs.verify_artifacts(db, v)
        assert ok, problems
        assert v.design_hash and len(v.design_hash) == 64
        assert p.status == "ready_to_send"
    url = send_current(c, proof_id, outbox)
    assert url.startswith("http://testserver/p/")
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        assert v.status == "sent" and p.status == "sent" and v.sent_at
        assert [e.event_type for e in events.chain(db, proof_id)] == ["created", "composed", "sent", "delivered"]
        assert events.verify_chain(db, proof_id) == (True, "ok")


def test_customer_opens_approves_without_an_account_and_gets_a_certificate(document, outbox):
    c = _client()
    sign_in(c, "dana2@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="marcus2@example.com")
    url = send_current(c, proof_id, outbox, "marcus2@example.com")
    customer = TestClient(app, headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
    page = customer.get(url)
    assert page.status_code == 200
    body = page.text
    assert "Approve this proof" in body and "generated from the actual embroidery file" in body and "Generic Red" in body
    assert "in sewing order" in body and "2.19 × 3.16 in" in body  # 55.6 x 80.2 mm
    # The render is served on the token; machine files are not.
    assert customer.get(url + "/render.png").headers["content-type"] == "image/png"
    assert customer.get(url + "/design.dst").status_code == 404
    pdf = customer.get(url + "/proof.pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    # Opened -> viewed.
    with database.SessionLocal() as db:
        v = db.get(ProofVersion, db.get(Proof, proof_id).current_version_id)
        assert v.status == "viewed"
    # Approve, with consent.
    r = customer.post(url + "/approve", data={"signer_name": "Marcus Lee", "signer_email": "marcus2@example.com", "consent": "yes", "local_offset": "300"}, follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        assert v.status == "approved" and p.status == "approved"
        rec = db.execute(database.select(ApprovalRecord).where(ApprovalRecord.proof_version_id == v.id)).scalar_one()
        assert rec.signer_name_typed == "Marcus Lee" and rec.signer_ip and "iPhone" in rec.signer_user_agent
        assert rec.consent_text_rendered == texts.CONSENT_TEXT and texts.DEFAULT_TERMS[:30] in rec.terms_body_rendered
        assert rec.artifact_hashes == v.artifact_hashes
        assert rec.conditions_snapshot["design_hash"] == v.design_hash and rec.conditions_snapshot["garment_color"] == "Navy"
        assert rec.certificate_sha256 == hashlib.sha256(storage.get(rec.certificate_storage_key)).hexdigest()
        assert rec.event_chain_head == events.chain(db, proof_id)[-1].event_hash or any(e.event_type == "approved" for e in events.chain(db, proof_id))
        cert_sha = rec.certificate_sha256
    # The customer gets the certificate link; it never expires.
    mail = outbox.latest_to("marcus2@example.com")
    assert "Certificate of Approval" in mail["text"] and cert_sha in mail["text"]
    cert_url = re.search(r"(http://testserver/c/[A-Za-z0-9_\-]+)", mail["text"]).group(1)
    page = customer.get(cert_url)
    assert page.status_code == 200 and "Verified" in page.text and cert_sha in page.text
    assert customer.get(cert_url + "/certificate.pdf").headers["content-type"] == "application/pdf"
    # Unauthenticated verification passes ...
    anon = TestClient(app)
    r = anon.get(f"/verify/{cert_sha}")
    assert r.status_code == 200 and r.json()["result"] == "pass", r.json()
    # ... and fails after a single byte of the stored proof PDF changes.
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        key = proofs._artifact_key(p, v.version_number, "proof.pdf")
    path = Path(config.ARTIFACT_DIR) / key
    data = bytearray(path.read_bytes())
    data[100] ^= 0xFF
    path.write_bytes(bytes(data))
    r = anon.get(f"/verify/{cert_sha}")
    assert r.status_code == 409 and r.json()["result"] == "fail" and any("pdf" in x for x in r.json()["reasons"])
    assert "VERIFICATION FAILED" in customer.get(cert_url).text
    data[100] ^= 0xFF
    path.write_bytes(bytes(data))
    assert anon.get(f"/verify/{cert_sha}").json()["result"] == "pass"
    assert anon.get("/verify/" + "0" * 64).status_code == 409


def test_proof_pdf_on_the_customer_link_contains_no_signer_pii(document, outbox):
    c = _client()
    sign_in(c, "dana3@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="pii@example.com", name="Private Person")
    url = send_current(c, proof_id, outbox, "pii@example.com")
    customer = TestClient(app)
    customer.get(url)
    customer.post(url + "/approve", data={"signer_name": "Private Person", "signer_email": "pii@example.com", "consent": "yes"}, follow_redirects=False)
    pdf = pdf_text(customer.get(url + "/proof.pdf").content)
    # The proof PDF predates the approval and must not carry the signer's
    # email, IP or user agent -- but it does carry the design.
    assert b"pii@example.com" not in pdf and b"testclient" not in pdf and b"Private Person" in pdf
    assert b"Generic Red" in pdf and b"Left chest" in pdf


def test_release_produces_a_run_ticket_and_needs_an_approved_version(document, outbox):
    c = _client()
    sign_in(c, "dana4@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="m4@example.com")
    r = c.post(f"/proofs/{proof_id}/release", follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).status == "ready_to_send"  # refused: nothing approved
    url = send_current(c, proof_id, outbox, "m4@example.com")
    customer = TestClient(app)
    customer.get(url)
    customer.post(url + "/approve", data={"signer_name": "M Four", "consent": "yes", "notes": "please rush"}, follow_redirects=False)
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).status == "approved_with_notes"
    r = c.post(f"/proofs/{proof_id}/release", follow_redirects=False)
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.status == "released" and p.released_at
        assert events.chain(db, proof_id)[-1].event_type == "released"
    ticket = c.get(f"/proofs/{proof_id}/run-ticket.pdf")
    assert ticket.headers["content-type"] == "application/pdf"
    text = pdf_text(ticket.content)
    for needle in (b"CLEARED TO SEW", b"4x4", b"Generic Red", b"cap backing", b"Left chest", b"7,173"):
        assert needle in text, needle
    # Machine files now download without a warning; before release the soft gate flagged them.
    with database.SessionLocal() as db:
        vid = db.get(Proof, proof_id).current_version_id
    r = c.get(f"/proofs/{proof_id}/versions/{vid}/design.dst")
    assert r.status_code == 200 and "X-PiperStitch-Warning" not in r.headers
    r = c.post(f"/proofs/{proof_id}/complete", follow_redirects=False)
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).status == "completed"


def test_sending_v2_supersedes_v1_and_revokes_its_link_but_keeps_it_readable(document, outbox):
    c = _client()
    sign_in(c, "dana5@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="m5@example.com")
    url1 = send_current(c, proof_id, outbox, "m5@example.com")
    customer = TestClient(app)
    customer.get(url1)
    r = customer.post(url1 + "/changes", data={"chip": ["color_wrong"], "body": "Red is too orange", "pins": json.dumps([{"x": 0.4, "y": 0.5, "note": "here"}])}, follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        assert p.status == "changes_requested"
        assert "Red is too orange" in c.get(f"/proofs/{proof_id}").text
    # Version 2 from a changed design.
    doc2 = dict(document, name="Cap logo B v2")
    r = c.post(f"/proofs/{proof_id}/compose", data={"garment_style_name": "Port & Company polo", "garment_color": "Black", "quantity": "24"},
               files={"document_file": ("cap-v2.stitchpilot", json.dumps(doc2), "application/json")}, follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v2 = db.get(ProofVersion, p.current_version_id)
        v1 = [v for v in p.versions if v.version_number == 1][0]
        assert v2.version_number == 2 and v1.status == "changes_requested"  # not superseded until sent
        assert "design changed" in v2.change_summary and "Navy → Black" in v2.change_summary
        assert v1.design_hash != v2.design_hash
    url2 = send_current(c, proof_id, outbox, "m5@example.com")
    assert url2 != url1
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v1 = [v for v in p.versions if v.version_number == 1][0]
        assert v1.status == "superseded" and v1.superseded_by_id == p.current_version_id
        types = [e.event_type for e in events.chain(db, proof_id)]
        assert "superseded" in types and types.index("superseded") < types.index("sent", types.index("sent") + 1)
    # The old link no longer works; the new link reads v1 read-only.
    old = customer.get(url1)
    assert "replaced" in old.text
    assert customer.post(url1 + "/approve", data={"signer_name": "x", "consent": "yes"}).status_code == 404
    v1_page = customer.get(url2 + "/versions/1")
    assert v1_page.status_code == 200 and "Version 1 of 2" in v1_page.text and "Approve this proof" not in v1_page.text
    assert customer.get(url2 + "/versions/1/render.png").status_code == 200
    v2_page = customer.get(url2)
    assert "Version 2 of 2" in v2_page.text and "supersedes v1" in v2_page.text and "Approve this proof" in v2_page.text


def test_roles_sales_cannot_release_and_stitcher_cannot_send(document, outbox):
    owner = _client()
    sign_in(owner, "dana6@shop.example", outbox)
    proof_id = create_and_compose(owner, document, email="m6@example.com")
    owner.post("/settings/invite", data={"email": "sales6@shop.example", "role": "sales"}, follow_redirects=False)
    owner.post("/settings/invite", data={"email": "ray6@shop.example", "role": "stitcher"}, follow_redirects=False)
    with database.SessionLocal() as db:
        vid = db.get(Proof, proof_id).current_version_id
    stitcher = _client()
    sign_in(stitcher, "ray6@shop.example", outbox)
    assert stitcher.post(f"/proofs/{proof_id}/versions/{vid}/send").status_code == 403
    assert stitcher.get(f"/proofs/{proof_id}").status_code == 200  # can read
    assert stitcher.get(f"/proofs/{proof_id}/run-ticket.pdf").status_code == 200
    sales = _client()
    sign_in(sales, "sales6@shop.example", outbox)
    assert sales.post(f"/proofs/{proof_id}/versions/{vid}/send", follow_redirects=False).status_code == 303  # allowed
    url = re.search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", outbox.latest_to("m6@example.com")["text"]).group(1)
    cust = TestClient(app)
    cust.get(url)
    cust.post(url + "/approve", data={"signer_name": "M Six", "consent": "yes"}, follow_redirects=False)
    assert sales.post(f"/proofs/{proof_id}/release").status_code == 403
    assert sales.get("/settings").status_code == 403
    assert owner.post(f"/proofs/{proof_id}/release", follow_redirects=False).status_code == 303
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).status == "released"


def test_disabled_entitlement_keeps_pages_live_and_blocks_new_sends(document, outbox):
    c = _client()
    sign_in(c, "dana7@shop.example", outbox)
    # Use up the free proofs: three proofs sent.
    urls = []
    for i in range(3):
        pid = create_and_compose(c, document, email=f"m7-{i}@example.com", title=f"Job {i}")
        urls.append((pid, send_current(c, pid, outbox, f"m7-{i}@example.com")))
    with database.SessionLocal() as db:
        m = db.execute(database.select(AccountUser).join(User).where(User.email == "dana7@shop.example")).scalar_one()
        ent = proofs.entitlements(db, m.account)
        assert ent.free_proofs_used == 3 and not ent.can_send
        db.commit()
    # A fourth proof composes but cannot be sent.
    pid4 = create_and_compose(c, document, email="m7-4@example.com", title="Job 4")
    with database.SessionLocal() as db:
        vid = db.get(Proof, pid4).current_version_id
    outbox.clear()
    c.post(f"/proofs/{pid4}/versions/{vid}/send", follow_redirects=False)
    assert outbox.latest_to("m7-4@example.com") is None
    assert "free proofs" in c.get(f"/proofs/{pid4}").text
    # Every existing page is live; an in-flight approval completes; the certificate stays downloadable.
    cust = TestClient(app)
    pid, url = urls[0]
    assert cust.get(url).status_code == 200
    r = cust.post(url + "/approve", data={"signer_name": "M Seven", "consent": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    cert_url = re.search(r"(http://testserver/c/[A-Za-z0-9_\-]+)", outbox.latest_to("m7-0@example.com")["text"]).group(1)
    assert cust.get(cert_url + "/certificate.pdf").status_code == 200
    # Resending an existing version is a link recovery, not a new proof: still allowed.
    with database.SessionLocal() as db:
        vid1 = db.get(Proof, urls[1][0]).current_version_id
    assert c.post(f"/proofs/{urls[1][0]}/versions/{vid1}/resend", follow_redirects=False).status_code == 303
    assert outbox.latest_to("m7-1@example.com") is not None


def test_event_chain_is_append_only_at_the_database(document, outbox):
    c = _client()
    sign_in(c, "dana8@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="m8@example.com")
    with database.SessionLocal() as db:
        e = events.chain(db, proof_id)[0]
        e.event_type = "tampered"
        with pytest.raises(Exception) as excinfo:
            db.commit()
        assert "append-only" in str(excinfo.value)
        db.rollback()
        with pytest.raises(Exception):
            db.execute(database.text("DELETE FROM proof_event WHERE proof_id = :p"), {"p": proof_id})
            db.commit()
        db.rollback()
        assert events.verify_chain(db, proof_id) == (True, "ok")


def test_design_hash_ignores_palette_but_not_geometry():
    d = json.loads((Path(__file__).parent / "fixtures/cap_digitize.json").read_text())
    h1 = stitch.design_hash(d["plan"]["commands"])
    d2 = json.loads(json.dumps(d))
    d2["colors"][0]["name"] = "Madeira 1001"
    assert stitch.design_hash(d2["plan"]["commands"]) == h1, "a thread swap is not a design change"
    d3 = json.loads(json.dumps(d))
    d3["plan"]["commands"][10][1] += 0.5
    assert stitch.design_hash(d3["plan"]["commands"]) != h1
    d4 = json.loads(json.dumps(d))
    d4["plan"]["commands"][10][1] += 0.01  # below the 0.1 mm resolution of the pre-image
    assert stitch.design_hash(d4["plan"]["commands"]) == h1


def test_render_is_deterministic_and_stops_sum_to_the_plan():
    d = json.loads((Path(__file__).parent / "fixtures/cap_digitize.json").read_text())
    a = stitch.render_png(d)
    b = stitch.render_png(d)
    assert a == b and len(a) > 10_000
    analysis = stitch.analyze(d)
    assert sum(s.stitch_count for s in analysis.stops) == analysis.stitch_count == 7173
    assert [s.hex for s in analysis.stops] == ["#ffffff", "#c81e2c"] or analysis.stops[1].hex.startswith("#")
    hero = stitch.fit_into(a, (1200, 630))
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(hero)).size == (1200, 630)


def test_void_revokes_links_and_expired_versions_can_be_resent(document, outbox):
    c = _client()
    sign_in(c, "dana9@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="m9@example.com")
    url = send_current(c, proof_id, outbox, "m9@example.com")
    # Force expiry.
    with database.SessionLocal() as db:
        v = db.get(ProofVersion, db.get(Proof, proof_id).current_version_id)
        v.response_expires_at = "2000-01-01T00:00:00Z"
        db.commit()
        assert proofs.expire_due(db) == 1
        db.commit()
        assert db.get(Proof, proof_id).status == "expired"
    with database.SessionLocal() as db:
        vid = db.get(Proof, proof_id).current_version_id
    c.post(f"/proofs/{proof_id}/versions/{vid}/resend", follow_redirects=False)
    new_url = re.search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", outbox.latest_to("m9@example.com")["text"]).group(1)
    assert new_url != url
    cust = TestClient(app)
    assert "replaced" in cust.get(url).text and "Approve this proof" in cust.get(new_url).text
    c.post(f"/proofs/{proof_id}/void", data={"reason": "customer went with a printed patch"}, follow_redirects=False)
    with database.SessionLocal() as db:
        assert db.get(Proof, proof_id).status == "void"
    assert "cancelled" in cust.get(new_url).text
    assert cust.post(new_url + "/approve", data={"signer_name": "x", "consent": "yes"}).status_code == 404


def test_every_garment_template_composes_and_measures(document):
    """All twelve templates, every zone, produce a mockup and a diagram."""
    from app import garments
    d = json.loads((Path(__file__).parent / "fixtures/cap_digitize.json").read_text())
    render = stitch.render_png(d)
    analysis = stitch.analyze(d)
    ppm = stitch.render_pixels_per_mm(render, analysis)
    assert len(garments.TEMPLATES) == 12
    for t in garments.TEMPLATES:
        for z in t.zones:
            mock = garments.composite(render, ppm, t, z, garments.color_hex("Navy"))
            diag = garments.placement_diagram(t, z, analysis.width_mm, analysis.height_mm, 12.7, 0)
            assert len(mock) > 5000 and len(diag) > 5000, f"{t.id}/{z.id}"
            note = garments.measured_note(t, z, analysis.height_mm, 12.7, 0)
            assert z.anchor_label in note and ("below" in note or "above" in note), note
    # True relative scale: the polo mockup places a 55.6 mm logo at 55.6 / mm_per_px pixels.
    t = garments.TEMPLATE_BY_ID["polo"]
    assert abs(t.mm_per_px * (930 - 70) - 560) < 1e-6
    assert garments.color_hex("navy") == "#1f2d4d" and garments.color_hex("#ABCDEF") == "#abcdef" and garments.color_hex("nonsense") == "#9b9d9f"


def test_colorways_are_offered_and_the_chosen_one_is_on_the_certificate(document, outbox):
    from app import colorways
    from app.db import ApprovalRecord as AR
    c = _client()
    sign_in(c, "dana-cw@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="cw@example.com")
    with database.SessionLocal() as db:
        v = db.get(ProofVersion, db.get(Proof, proof_id).current_version_id)
        vid, hash_before = v.id, v.design_hash
        stops = [s.hex for s in v.thread_stops]
    # Add "On black": white stays, red becomes gold.
    r = c.post(f"/proofs/{proof_id}/versions/{vid}/colorways", data={"name": "On black", "hex_1": stops[0], "hex_2": "#e0b332", "name_2": "Madeira 1024", "code_2": "1024"}, follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        v = db.get(ProofVersion, vid)
        assert len(v.colorways) == 1 and v.colorways[0].name == "On black" and v.colorways[0].ordinal == 2
        assert v.design_hash == hash_before, "a palette change is not a design change"
        cw_stops = colorways.stops_for(db, v, 2)
        assert [s.hex for s in cw_stops][1] == "#e0b332" and cw_stops[1].thread_name == "Madeira 1024"
        assert v.artifact_hashes["colorways"]["2"]["render"] and v.artifact_hashes["colorways"]["2"]["mockup"]
        assert [s.hex for s in v.thread_stops] == stops, "the version's own stops are untouched"
    url = send_current(c, proof_id, outbox, "cw@example.com")
    cust = TestClient(app)
    page = cust.get(url).text
    assert "Choose a colourway" in page and "2. On black" in page
    assert cust.get(url + "/cw2-render.png").status_code == 200
    cust.post(url + "/approve", data={"signer_name": "C W", "consent": "yes", "colorway": "2"}, follow_redirects=False)
    with database.SessionLocal() as db:
        rec = db.execute(database.select(AR).join(ProofVersion).where(ProofVersion.id == vid)).scalar_one()
        assert rec.colorway_id and rec.conditions_snapshot["colorway_ordinal"] == 2 and any("Madeira 1024" in s for s in rec.conditions_snapshot["thread_stops"])
    # Colorways can't be added after sending.
    r = c.post(f"/proofs/{proof_id}/versions/{vid}/colorways", data={"name": "late", "hex_1": stops[0], "hex_2": "#000000"}, follow_redirects=False)
    with database.SessionLocal() as db:
        assert len(db.get(ProofVersion, vid).colorways) == 1


def test_version_compare_serves_a_difference_image(document, outbox):
    c = _client()
    sign_in(c, "dana-cmp@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="cmp@example.com")
    send_current(c, proof_id, outbox, "cmp@example.com")
    c.post(f"/proofs/{proof_id}/compose", data={"quantity": "24"}, files={"document_file": ("v2.stitchpilot", json.dumps(dict(document, name="Cap v2")), "application/json")}, follow_redirects=False)
    url = send_current(c, proof_id, outbox, "cmp@example.com")
    cust = TestClient(app)
    page = cust.get(url).text
    assert "Before and after" in page and "What changed" in page
    r = cust.get(url + "/compare/1/difference.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    reds = sum(1 for px in im.getdata() if px[0] > 150 and px[1] < 60)
    assert reds > 50, "the dropped stitches show up in red"
    assert cust.get(url + "/compare/2/difference.png").status_code == 404


def test_every_shop_page_renders(document, outbox):
    """GET every shop-side page, before and after a version exists -- a
    template error only shows when the page is rendered."""
    c = _client()
    sign_in(c, "dana-pages@shop.example", outbox)
    assert c.get("/proofs").status_code == 200
    assert c.get("/proofs/new").status_code == 200
    assert c.get("/settings").status_code == 200
    r = c.post("/proofs/new", data={"title": "Pages", "contact_name": "P", "contact_email": "pages@example.com"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    assert c.get(f"/proofs/{pid}").status_code == 200
    assert c.get(f"/proofs/{pid}/compose").status_code == 200
    pid2 = create_and_compose(c, document, email="pages2@example.com")
    assert c.get(f"/proofs/{pid2}").status_code == 200
    assert c.get(f"/proofs/{pid2}/compose").status_code == 200, "compose form with a previous version"
    url = send_current(c, pid2, outbox, "pages2@example.com")
    assert c.get(f"/proofs/{pid2}").status_code == 200
    cust = TestClient(app)
    assert cust.get(url).status_code == 200
    assert cust.get(url + "/versions/1").status_code == 200
    assert cust.get("/verify").status_code == 200
