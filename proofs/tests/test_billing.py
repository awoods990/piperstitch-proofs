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
    assert "$25/month" in shop.get("/settings").text and "Manage billing" in shop.get("/settings").text
    # Subscription cancelled -> entitlement off, existing proof page still live.
    r = hook.post("/webhooks/stripe", content=_event("evt_2", "customer.subscription.deleted", {"object": "subscription", "id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {"proofs_account_id": account_id}}))
    assert "canceled" in r.json()["result"]
    with database.SessionLocal() as db:
        ent = db.get(database.AccountEntitlements, account_id)
        assert not ent.proofs_enabled and not ent.can_send
    url = __import__("re").search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", outbox.latest_to("b@example.com")["text"]).group(1)
    assert TestClient(app).get(url).status_code == 200
    assert "Upgrade to Proofs" in shop.get("/settings").text
