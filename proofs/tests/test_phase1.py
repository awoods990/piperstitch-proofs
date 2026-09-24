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
    for needle in (b"CLEARED TO SEW", b"4x4", b"Generic Red", b"cap backing", b"Left chest", b"7,173", b"please rush", b"80/12", b"Checklist", b"M \\327 10"):
        assert needle in text, needle
    # The job page leads with printing the sheet.
    assert "Print the production sheet" in c.get(f"/proofs/{proof_id}").text
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
    """Every template (eight of them headwear), every zone, produces a mockup and a diagram."""
    from app import garments
    d = json.loads((Path(__file__).parent / "fixtures/cap_digitize.json").read_text())
    render = stitch.render_png(d)
    analysis = stitch.analyze(d)
    ppm = stitch.render_pixels_per_mm(render, analysis)
    assert len(garments.TEMPLATES) == 17 and sum(1 for t in garments.TEMPLATES if t.category == "cap") == 8
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


def test_shop_logo_appears_on_customer_pages_emails_and_the_proof_pdf(document, outbox):
    import io
    from PIL import Image
    c = _client()
    sign_in(c, "dana-logo@shop.example", outbox)
    png = io.BytesIO()
    Image.new("RGBA", (300, 100), (26, 111, 209, 255)).save(png, format="PNG")
    r = c.post("/settings/logo", files={"logo": ("logo.png", png.getvalue(), "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    settings = c.get("/settings").text
    assert "Replace logo" in settings
    logo_url = re.search(r"/brand/([a-f0-9]+)/logo", settings).group(0)
    r = c.get(logo_url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (300, 100)
    proof_id = create_and_compose(c, document, email="logo-cust@example.com")
    url = send_current(c, proof_id, outbox, "logo-cust@example.com")
    mail = outbox.latest_to("logo-cust@example.com")
    assert f"http://testserver{logo_url}" in mail["html"]
    customer = TestClient(app)
    page = customer.get(url).text
    assert logo_url in page and "Cancel this job" not in page and "Decline" not in page
    pdf = customer.get(url + "/proof.pdf").content
    assert pdf.count(b"/Subtype /Image") >= 2   # render (+ mockup) and the logo
    # Remove it and the pages go back to name only.
    c.post("/settings/logo", data={"remove": "yes"}, follow_redirects=False)
    assert logo_url not in customer.get(url).text and c.get(logo_url).status_code == 404


def test_moving_the_design_before_sending_updates_every_document(document, outbox):
    c = _client()
    sign_in(c, "dana-move@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="move@example.com", placement_down="0", placement_across="0")
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        vid, before = v.id, dict(v.artifact_hashes)
        note_before = v.placement_notes
        assert v.artifact_rev == 0
    page = c.get(f"/proofs/{proof_id}").text
    assert "Drag the logo" in page and 'data-ppm="' in page
    # Drag it 1 in down and 0.5 in to the right.
    r = c.post(f"/proofs/{proof_id}/versions/{vid}/placement", data={"down": "1", "across": "0.5", "units": "in"}, follow_redirects=False)
    assert r.status_code == 303
    with database.SessionLocal() as db:
        v = db.get(ProofVersion, vid)
        assert v.artifact_rev == 1 and abs(v.placement_down_mm - 25.4) < 0.01 and abs(v.placement_across_mm - 12.7) < 0.01
        after = v.artifact_hashes
        # Placement-dependent artifacts changed; the stitches and machine files did not.
        assert after["mockup"] != before["mockup"] and after["diagram"] != before["diagram"] and after["pdf"] != before["pdf"]
        assert after["render"] == before["render"] and after["machine_files"] == before["machine_files"]
        assert v.placement_notes != note_before and "4.0 in below" in v.placement_notes and "4.1 in left" in v.placement_notes
        ok, problems = proofs.verify_artifacts(db, v)
        assert ok, problems
        assert events.chain(db, proof_id)[-1].event_type == "repositioned"
    # The served mockup/diagram/PDF are the moved ones; the run ticket and proof PDF carry the new note.
    assert hashlib.sha256(c.get(f"/proofs/{proof_id}/versions/{vid}/mockup.png").content).hexdigest() == after["mockup"]
    assert b"4.0 in below" in pdf_text(c.get(f"/proofs/{proof_id}/versions/{vid}/proof.pdf").content)
    assert b"4.0 in below" in pdf_text(c.get(f"/proofs/{proof_id}/run-ticket.pdf").content)
    # Once sent, the position is frozen.
    url = send_current(c, proof_id, outbox, "move@example.com")
    assert "4.0 in below" in TestClient(app).get(url).text
    r = c.post(f"/proofs/{proof_id}/versions/{vid}/placement", data={"down": "0", "across": "0", "units": "in"}, follow_redirects=False)
    with database.SessionLocal() as db:
        assert db.get(ProofVersion, vid).artifact_rev == 1
    assert "before the proof is sent" in c.get(f"/proofs/{proof_id}").text


def test_core_hoops_and_thread_library_prepopulate_and_the_garment_can_change_before_sending(document, outbox):
    from app import core_client
    c = _client()
    sign_in(c, "dana-prefs@shop.example", outbox)
    core_client.license_admin.preferences = {"defaultHoopName": 'Mighty Hoop 5.5" × 5.5"',
                                             "threadLibrary": [{"id": "t1", "name": "Madeira Polyneon 1147", "brand": "Madeira", "catalogNumber": "1147", "rgb": {"r": 200, "g": 30, "b": 40}}]}
    try:
        r = c.post("/proofs/new", data={"title": "Prefs", "contact_name": "P", "contact_email": "prefs@example.com"}, follow_redirects=False)
        pid = r.headers["location"].rsplit("/", 1)[1]
        with database.SessionLocal() as db:
            m = db.execute(database.select(AccountUser).join(User).where(User.email == "dana-prefs@shop.example")).scalar_one()
            m.core_session_token = "tok"   # signed in through PiperStitch
            db.commit()
        page = c.get(f"/proofs/{pid}/compose").text
        assert 'value="Mighty Hoop 5.5&#34; × 5.5&#34;" selected' in page and "Your PiperStitch default is preselected" in page
        # A hoop the design doesn't fit is refused with a plain explanation.
        form = {"garment_template_id": "structured_cap", "garment_zone": "front", "garment_color": "Black", "quantity": "1", "hoop_code": "Cap frame"}
        r = c.post(f"/proofs/{pid}/compose", data=form, files={"document_file": ("cap.stitchpilot", json.dumps(document), "application/json")}, follow_redirects=False)
        assert r.headers["location"].endswith("/compose")
        assert "fit the Cap frame hoop" in c.get(f"/proofs/{pid}/compose").text
        form["hoop_code"] = '4" × 4"'
        r = c.post(f"/proofs/{pid}/compose", data=form, files={"document_file": ("cap.stitchpilot", json.dumps(document), "application/json")}, follow_redirects=False)
        assert r.headers["location"] == f"/proofs/{pid}"
        page = c.get(f"/proofs/{pid}").text
        # The thread library feeds the colourway form; the garment switcher is offered.
        assert 'datalist id="threadLib"' in page and 'value="Madeira Polyneon 1147"' in page and 'data-hex="#c81e28"' in page
        assert "Show it on a different garment" in page and "Trucker cap" in page and "Flat-bill snapback" in page
        with database.SessionLocal() as db:
            vid = db.get(Proof, pid).current_version_id
        r = c.post(f"/proofs/{pid}/versions/{vid}/garment", data={"garment_template_id": "trucker_cap", "garment_zone": "front", "garment_color": "Red"}, follow_redirects=False)
        assert r.status_code == 303
        with database.SessionLocal() as db:
            v = db.get(ProofVersion, vid)
            assert v.garment_template_id == "trucker_cap" and v.garment_color == "Red" and v.garment_style_name == "Trucker cap (mesh back)" and v.hoop_code == '4" × 4"'
            assert v.artifact_rev == 1 and v.placement_name == "Front (foam) panel"
            assert events.chain(db, pid)[-1].event_type == "restyled"
            ok, problems = proofs.verify_artifacts(db, v)
            assert ok, problems
        assert b"Trucker cap" in pdf_text(c.get(f"/proofs/{pid}/versions/{vid}/proof.pdf").content)
    finally:
        core_client.license_admin.preferences = None


def test_handoff_signs_in_from_piperstitch_and_back_without_a_second_code(document, outbox):
    """Arriving from the app with a one-time code creates the same owner
    account an email code would; /core/open goes back the same way."""
    from app import core_client
    la = core_client.license_admin
    la.sessions["core-tok-1"] = {"customer_id": 4242, "email": "handoff@shop.example", "name": "Hana Doff"}
    code = la.create_handoff("core-tok-1", target="proofs")
    c = _client()
    r = c.get(f"/signin/handoff?code={code}&next=/proofs/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/proofs/new"
    assert "Hana Doff" in c.get("/proofs").text or "handoff@shop.example" in c.get("/proofs").text
    with database.SessionLocal() as db:
        m = db.execute(database.select(AccountUser).join(User).where(User.email == "handoff@shop.example")).scalar_one()
        assert m.role == "owner" and m.core_session_token == "core-tok-1-via-proofs" and m.account.core_customer_id == 4242
    # Used codes and junk are refused with a message, not a crash.
    r = c.get(f"/signin/handoff?code={code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin" and "expired" in c.get("/signin").text
    # Back into PiperStitch: a fresh handoff carrying the project to open.
    r = c.get("/core/open?project=abc-123&return=https%3A%2F%2Fproofs.test%2Fproofs%2F1", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://app.piperstitch.com/?handoff=hand-") and "project=abc-123" in loc and "return=https%3A%2F%2Fproofs.test" in loc
    assert la.last_handoff == ("core-tok-1-via-proofs", "core")
    # The top bar offers the way back.
    assert 'href="/core/open"' in c.get("/proofs").text
    # The sign-in email's link (?email=&code=) verifies without retyping; a wrong code shows the code step.
    c2 = _client()
    c2.post("/signin", data={"email": "link@shop.example"})
    mail = outbox.latest_to("link@shop.example")
    code6 = re.search(r"code is (\d{6})", mail["text"]).group(1)
    r = c2.get(f"/signin?email=link%40shop.example&code={code6}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/proofs"
    assert c2.get("/proofs").status_code == 200
    c3 = _client()
    c3.post("/signin", data={"email": "link2@shop.example"})
    assert "isn" in c3.get("/signin?email=link2%40shop.example&code=000000").text


def test_guided_setup_answers_from_piperstitch_prefill_the_account_once_per_run(document, outbox):
    """PiperStitch's guided setup writes the shop's answers into the shared
    preferences; the first sign-in here copies them onto the account. The
    same run is never re-applied (settings edited here stand), but a
    fresh run -- a new `completedAt` -- is."""
    from app import core_client
    from app.db import Account
    la = core_client.license_admin
    la.sessions["core-tok-setup"] = {"customer_id": 5151, "email": "setup@shop.example", "name": "Sid Setup"}
    la.preferences = {
        "units": "in",
        "business": {"name": "Sid's Stitches", "phone": "555-0100", "machineBrands": ["brother"]},
        "proofsDefaults": {"shopName": "", "replyTo": "orders@shop.example", "responseWindowDays": 5, "remindersEnabled": False, "releaseGate": "hard"},
        "onboarding": {"version": 1, "completedAt": "2026-09-16T20:00:00Z", "skippedAt": None, "products": ["core", "proofs"]},
    }
    try:
        c = _client()
        code = la.create_handoff("core-tok-setup", target="proofs")
        assert c.get(f"/signin/handoff?code={code}", follow_redirects=False).status_code == 303
        with database.SessionLocal() as db:
            a = db.execute(database.select(Account).where(Account.core_customer_id == 5151)).scalar_one()
            assert a.shop_name == "Sid's Stitches" and a.reply_to_email == "orders@shop.example" and a.phone == "555-0100"
            assert a.units == "imperial" and a.default_response_window_days == 5 and a.reminders_enabled is False and a.release_gate_policy == "hard"
            assert a.core_setup_applied == "2026-09-16T20:00:00Z"
        # Edited here, then signed in again with the same setup run: untouched.
        c.post("/settings", data={"shop_name": "Sid's Stitches LLC", "reply_to_email": "orders@shop.example", "phone": "555-0100", "release_gate_policy": "hard",
                                  "default_response_window_days": "9", "quiet_hours_start": "20", "quiet_hours_end": "8", "reminders_enabled": "no"}, follow_redirects=False)
        code = la.create_handoff("core-tok-setup", target="proofs")
        assert _client().get(f"/signin/handoff?code={code}", follow_redirects=False).status_code == 303
        with database.SessionLocal() as db:
            a = db.execute(database.select(Account).where(Account.core_customer_id == 5151)).scalar_one()
            assert a.shop_name == "Sid's Stitches LLC" and a.default_response_window_days == 9
        # Guided setup run again over there: the new answers apply.
        la.preferences["proofsDefaults"]["shopName"] = "Sid's Embroidery"
        la.preferences["units"] = "cm"
        la.preferences["onboarding"]["completedAt"] = "2026-09-17T09:00:00Z"
        code = la.create_handoff("core-tok-setup", target="proofs")
        assert _client().get(f"/signin/handoff?code={code}", follow_redirects=False).status_code == 303
        with database.SessionLocal() as db:
            a = db.execute(database.select(Account).where(Account.core_customer_id == 5151)).scalar_one()
            assert a.shop_name == "Sid's Embroidery" and a.units == "metric" and a.core_setup_applied == "2026-09-17T09:00:00Z"
    finally:
        la.preferences = None


def test_settings_test_email_reports_the_transport_and_its_refusal(outbox, monkeypatch):
    """The Settings page says how mail leaves the server and a test send
    puts the transport's own reason on the page -- an owner whose proofs
    'aren't coming through' needs to read 'SendAsDenied', not a log line."""
    from app import emailer
    c = _client()
    sign_in(c, "mail-test@shop.example", outbox)
    page = c.get("/settings").text
    assert "Outgoing email" in page and "Send a test email to mail-test@shop.example" in page
    assert c.get("/health").json()["email"]["transport"] == "outbox"

    r = c.post("/settings/test-email", follow_redirects=True)
    assert "Test email sent to mail-test@shop.example" in r.text
    assert any(m["subject"].startswith("PiperStitch Proofs test email") for m in outbox.all())

    monkeypatch.setattr(emailer, "send", lambda **kw: emailer._fail("SMTP send failed: 550 5.7.60 SendAsDenied"))
    r = c.post("/settings/test-email", follow_redirects=True)
    assert "was not sent" in r.text and "SendAsDenied" in r.text


def test_the_production_panel_offers_the_machine_file_beside_the_sheet(document, outbox):
    """The sheet tells the operator what to do; the file is what they load
    into the machine. Both belong at the same weight at the moment someone
    is walking to the machine, not one of them buried in a version panel."""
    c = _client()
    sign_in(c, "dana9@shop.example", outbox)
    proof_id = create_and_compose(c, document, email="m9@example.com")
    url = send_current(c, proof_id, outbox, "m9@example.com")
    customer = TestClient(app)
    customer.get(url)
    customer.post(url + "/approve", data={"signer_name": "M Nine", "consent": "yes"}, follow_redirects=False)

    page = c.get(f"/proofs/{proof_id}").text
    assert "Download the machine file" in page
    assert "Pick the format your machine reads" in page
    for fmt in core_client.MACHINE_FORMATS:
        assert f"/design.{fmt}" in page, fmt

    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        vid, reference = p.current_version_id, p.reference
    r = c.get(f"/proofs/{proof_id}/versions/{vid}/design.dst")
    assert r.status_code == 200
    # An attachment, named so a shop can find it again -- not "design.dst".
    assert r.headers["content-disposition"] == f'attachment; filename="{reference}-v1.dst"'


def test_a_hard_release_gate_offers_no_download_until_the_job_is_released(document, outbox):
    c = _client()
    sign_in(c, "dana10@shop.example", outbox)
    c.post("/settings", data={"shop_name": "Hard Gate Co", "release_gate_policy": "hard"}, follow_redirects=False)
    proof_id = create_and_compose(c, document, email="m10@example.com")
    url = send_current(c, proof_id, outbox, "m10@example.com")
    customer = TestClient(app)
    customer.get(url)
    customer.post(url + "/approve", data={"signer_name": "M Ten", "consent": "yes"}, follow_redirects=False)

    page = c.get(f"/proofs/{proof_id}").text
    assert "Release the job to unlock the machine files" in page
    assert "Pick the format your machine reads" not in page, "a chooser that only 423s is worse than none"

    with database.SessionLocal() as db:
        vid = db.get(Proof, proof_id).current_version_id
    assert c.get(f"/proofs/{proof_id}/versions/{vid}/design.dst").status_code == 423

    c.post(f"/proofs/{proof_id}/release", follow_redirects=False)
    assert "Pick the format your machine reads" in c.get(f"/proofs/{proof_id}").text
    assert c.get(f"/proofs/{proof_id}/versions/{vid}/design.dst").status_code == 200
