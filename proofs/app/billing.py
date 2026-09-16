"""The Proofs add-on subscription through Stripe -- the same account and
conventions as License Admin (Checkout in subscription mode, the Billing
Portal for changes, webhooks idempotent by event id). Entitlement lives
on `account_entitlements`: `proofs_enabled` mirrors the subscription's
state; a lapse leaves everything readable and blocks new sends (PRD
"Entitlement model").
"""

from __future__ import annotations

import logging
from typing import Optional

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, proofs
from .db import Account, AccountEntitlements, StripeEvent, utcnow

log = logging.getLogger("proofs.billing")

ACTIVE_STATUSES = ("active", "trialing", "past_due")


def configured() -> bool:
    return bool(config.STRIPE_SECRET_KEY and config.STRIPE_PRICE_PROOFS)


def _client():
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def checkout_url(db: Session, account: Account, *, email: str, base_url: str) -> str:
    if not configured():
        raise proofs.TransitionError("Billing isn't configured on this server yet.")
    s = _client()
    ent = proofs.entitlements(db, account)
    params = {
        "mode": "subscription",
        "line_items": [{"price": config.STRIPE_PRICE_PROOFS, "quantity": 1}],
        "success_url": f"{base_url}/settings?upgraded=1",
        "cancel_url": f"{base_url}/settings",
        "client_reference_id": account.id,
        "metadata": {"proofs_account_id": account.id},
        "subscription_data": {"metadata": {"proofs_account_id": account.id}},
        "allow_promotion_codes": True,
    }
    if ent.stripe_customer_id:
        params["customer"] = ent.stripe_customer_id
    else:
        params["customer_email"] = email
    session = s.checkout.Session.create(**params)
    return session.url


def portal_url(db: Session, account: Account, *, base_url: str) -> str:
    ent = proofs.entitlements(db, account)
    if not ent.stripe_customer_id:
        raise proofs.TransitionError("No billing on file yet.")
    return _client().billing_portal.Session.create(customer=ent.stripe_customer_id, return_url=f"{base_url}/settings").url


def handle_webhook(db: Session, payload: bytes, signature: str) -> str:
    """Returns what was done, for the log. Idempotent by event id."""
    if config.STRIPE_WEBHOOK_SECRET:
        event = stripe.Webhook.construct_event(payload, signature, config.STRIPE_WEBHOOK_SECRET)
    else:
        import json
        event = stripe.Event.construct_from(json.loads(payload), config.STRIPE_SECRET_KEY or "sk_test")
    if db.get(StripeEvent, event.id) is not None:
        return "duplicate"
    db.add(StripeEvent(id=event.id, type=event.type))
    obj = event.data.object
    kind = event.type
    if kind == "checkout.session.completed":
        account_id = (obj.get("metadata") or {}).get("proofs_account_id") or obj.get("client_reference_id")
        account = db.get(Account, account_id) if account_id else None
        if account is None:
            return "no account"
        ent = proofs.entitlements(db, account)
        ent.stripe_customer_id = obj.get("customer") or ent.stripe_customer_id
        ent.stripe_subscription_id = obj.get("subscription") or ent.stripe_subscription_id
        ent.proofs_enabled = True
        ent.plan_code = "proofs"
        return "enabled"
    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        account = _account_for_subscription(db, obj)
        if account is None:
            return "no account"
        ent = proofs.entitlements(db, account)
        ent.stripe_subscription_id = obj.get("id")
        ent.stripe_customer_id = obj.get("customer") or ent.stripe_customer_id
        status = obj.get("status")
        ent.subscription_status = status or ""
        ent.proofs_enabled = status in ACTIVE_STATUSES and kind != "customer.subscription.deleted"
        ent.plan_code = "proofs" if ent.proofs_enabled else "free"
        ent.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        return f"subscription {status}"
    return "ignored"


def _account_for_subscription(db: Session, sub) -> Optional[Account]:
    account_id = (sub.get("metadata") or {}).get("proofs_account_id")
    if account_id:
        a = db.get(Account, account_id)
        if a:
            return a
    customer = sub.get("customer")
    if customer:
        ent = db.execute(select(AccountEntitlements).where(AccountEntitlements.stripe_customer_id == customer)).scalar_one_or_none()
        if ent:
            return db.get(Account, ent.account_id)
    return None
