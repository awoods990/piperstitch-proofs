"""The add-on subscription: webhooks flip the entitlement, idempotently;
a lapse blocks new sends but changes nothing else (PRD Entitlement model)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as database, proofs
from app.db import AccountUser, User
from app.main import app
from tests.test_phase1 import create_and_compose, send_current, sign_in


def _event(id_, type_, obj):
    return json.dumps({"id": id_, "object": "event", "type": type_, "data": {"object": obj}, "api_version": "2024-06-20"}).encode()


def test_checkout_and_subscription_webhooks_flip_the_entitlement(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-bill@shop.example", outbox)
    with database.SessionLocal() as db:
        m = db.execute(select(AccountUser).join(User).where(User.email == "dana-bill@shop.example")).scalar_one()
        account_id = m.account_id
        ent = proofs.entitlements(db, m.account)
        ent.free_proofs_used = ent.free_proofs_granted   # out of free proofs
        db.commit()
    pid = create_and_compose(shop, document, email="b@example.com")
    with database.SessionLocal() as db:
        vid = db.get(database.Proof, pid).current_version_id
    outbox.clear()
    shop.post(f"/proofs/{pid}/versions/{vid}/send", follow_redirects=False)
    assert outbox.latest_to("b@example.com") is None
    hook = TestClient(app)
    r = hook.post("/webhooks/stripe", content=_event("evt_1", "checkout.session.completed", {"object": "checkout.session", "customer": "cus_1", "subscription": "sub_1", "metadata": {"proofs_account_id": account_id}}))
    assert r.json()["result"] == "enabled"
    assert hook.post("/webhooks/stripe", content=_event("evt_1", "checkout.session.completed", {})).json()["result"] == "duplicate"
    with database.SessionLocal() as db:
        ent = db.get(database.AccountEntitlements, account_id)
        assert ent.proofs_enabled and ent.stripe_customer_id == "cus_1" and ent.can_send
    send_current(shop, pid, outbox, "b@example.com")   # now allowed
    assert "$24/month" in shop.get("/settings").text and "Manage billing" in shop.get("/settings").text
    # Subscription cancelled -> entitlement off, existing proof page still live.
    r = hook.post("/webhooks/stripe", content=_event("evt_2", "customer.subscription.deleted", {"object": "subscription", "id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {"proofs_account_id": account_id}}))
    assert "canceled" in r.json()["result"]
    with database.SessionLocal() as db:
        ent = db.get(database.AccountEntitlements, account_id)
        assert not ent.proofs_enabled and not ent.can_send
    url = __import__("re").search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", outbox.latest_to("b@example.com")["text"]).group(1)
    assert TestClient(app).get(url).status_code == 200
    assert "Upgrade to Proofs" in shop.get("/settings").text


def test_plan_is_license_admins_when_the_shop_signed_in_through_piperstitch(document, outbox):
    """The owner signed in via PiperStitch: three free proofs counted in
    License Admin, checkout and the portal go there, a lapse blocks sends."""
    from app import core_client
    la = core_client.license_admin
    shop = TestClient(app)
    sign_in(shop, "dana-la@shop.example", outbox)
    with database.SessionLocal() as db:
        m = db.execute(select(AccountUser).join(User).where(User.email == "dana-la@shop.example")).scalar_one()
        m.core_session_token = "tok-la"
        account_id = m.account_id
        db.commit()
    la.proofs_plans.pop("tok-la", None)
    page = shop.get("/settings").text
    assert "3 of 3 free proofs left" in page.replace("</b>", "").replace("<b>", "") and "Billed alongside your PiperStitch subscription" in page
    pids = []
    for i in range(3):
        pid = create_and_compose(shop, document, email=f"la-{i}@example.com", title=f"LA {i}")
        send_current(shop, pid, outbox, f"la-{i}@example.com")
        pids.append(pid)
    assert la.proofs_plans["tok-la"]["free_used"] == 3
    with database.SessionLocal() as db:
        ent = db.get(database.AccountEntitlements, account_id)
        assert ent.managed_by == "license_admin" and ent.free_proofs_used == 3 and not ent.can_send
    # A fresh link on an already-sent proof isn't a new proof; a fourth job is refused.
    with database.SessionLocal() as db:
        vid = db.get(database.Proof, pids[0]).current_version_id
    outbox.clear()
    shop.post(f"/proofs/{pids[0]}/versions/{vid}/resend", follow_redirects=False)
    assert outbox.latest_to("la-0@example.com") is not None
    pid4 = create_and_compose(shop, document, email="la-4@example.com", title="LA 4")
    with database.SessionLocal() as db:
        vid4 = db.get(database.Proof, pid4).current_version_id
    outbox.clear()
    shop.post(f"/proofs/{pid4}/versions/{vid4}/send", follow_redirects=False)
    assert outbox.latest_to("la-4@example.com") is None and "free proofs" in shop.get(f"/proofs/{pid4}").text
    # Upgrade goes to License Admin's checkout, returning to Settings.
    r = shop.post("/billing/checkout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.test/proofs"
    assert la.last_checkout[0] == "tok-la" and la.last_checkout[1].endswith("/settings?subscribed=1")
    # Stripe's webhook lands in License Admin; coming back with ?subscribed=1 refreshes the mirror.
    la.proofs_plans["tok-la"].update({"subscribed": True, "status": "active", "has_billing": True})
    page = shop.get("/settings?subscribed=1").text
    assert "subscribed to PiperStitch Proofs" in page and "Manage billing" in page and "$24/month" in page
    send_current(shop, pid4, outbox, "la-4@example.com")
    assert la.proofs_plans["tok-la"]["free_used"] == 3   # subscribers aren't counted
    r = shop.post("/billing/portal", follow_redirects=False)
    assert r.headers["location"] == "https://billing.stripe.test/portal"
    # License Admin unreachable: the last mirror stands, sends carry on.
    real = la.proofs_use
    la.proofs_use = lambda token, ref: (_ for _ in ()).throw(core_client.CoreError("License Admin is unreachable: boom"))
    try:
        pid5 = create_and_compose(shop, document, email="la-5@example.com", title="LA 5")
        send_current(shop, pid5, outbox, "la-5@example.com")
    finally:
        la.proofs_use = real
