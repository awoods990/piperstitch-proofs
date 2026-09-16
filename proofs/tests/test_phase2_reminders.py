"""The chase engine (PRD 5.7, Phase 2 acceptance: cadences fire on
schedule, respect quiet hours, suppress for opt-out and snooze, cancel on
customer action)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as database, reminders
from app.db import Account, Contact, Proof, ProofVersion, ReminderSend
from app.main import app
from tests.test_phase1 import create_and_compose, send_current, sign_in


def _sent_reminders(db, proof_id):
    return list(db.execute(select(ReminderSend).where(ReminderSend.proof_id == proof_id)).scalars())


def _set_sent_ago(proof_id, hours, relative_to=None):
    """sent_at = (the fake clock the test will run with) - hours."""
    base = relative_to or _noon_for()
    with database.SessionLocal() as db:
        p = db.get(Proof, proof_id)
        v = db.get(ProofVersion, p.current_version_id)
        v.sent_at = (base - timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        db.commit()


def _noon_for(tz="America/New_York"):
    """A UTC instant that is 12:00 local in the contact's zone today."""
    from zoneinfo import ZoneInfo
    local = datetime.now(ZoneInfo(tz)).replace(hour=12, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def _late_night_for(tz="America/New_York"):
    from zoneinfo import ZoneInfo
    local = datetime.now(ZoneInfo(tz)).replace(hour=23, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def test_response_cadence_fires_on_schedule_and_cancels_on_customer_action(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-chase@shop.example", outbox)
    pid = create_and_compose(shop, document, email="quiet@example.com")
    send_current(shop, pid, outbox, "quiet@example.com")
    outbox.clear()
    # Day 1: nothing due.
    with database.SessionLocal() as db:
        assert reminders.run_due(db, now=_noon_for()) == {"sent": 0, "suppressed": 0}
        db.commit()
    # 3 days later, unopened: the 48 h "not opened" step fires once, with a fresh link.
    _set_sent_ago(pid, 72)
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for())
        db.commit()
    assert r["sent"] == 1
    mail = outbox.latest_to("quiet@example.com")
    assert mail and "waiting" in mail["subject"]
    new_url = re.search(r"(http://testserver/p/[A-Za-z0-9_\-]+)", mail["text"]).group(1)
    # Same day again: nothing more (fired steps don't repeat; one per day).
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for() + timedelta(hours=1))
        db.commit()
        assert r["sent"] == 0
    # The reminder's link works; the customer opens, then approves -> no more reminders ever.
    customer = TestClient(app)
    assert customer.get(new_url).status_code == 200 and "Approve this proof" in customer.get(new_url).text
    customer.post(new_url + "/approve", data={"signer_name": "Q Customer", "consent": "yes"}, follow_redirects=False)
    outbox.clear()
    _set_sent_ago(pid, 24 * 20, relative_to=_noon_for() + timedelta(days=1))
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for() + timedelta(days=1))
        db.commit()
    assert outbox.latest_to("quiet@example.com") is None


def test_opened_no_response_step_quiet_hours_snooze_and_opt_out(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-chase2@shop.example", outbox)
    pid = create_and_compose(shop, document, email="slow@example.com")
    url = send_current(shop, pid, outbox, "slow@example.com")
    TestClient(app).get(url)   # opened -> viewed
    outbox.clear()
    _set_sent_ago(pid, 72)
    # Opened, so the "not opened" step is suppressed (condition), and the
    # 96 h "opened, no response" step isn't due yet.
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for())
        db.commit()
        assert r["sent"] == 0 and r["suppressed"] == 1
        assert [s.suppressed_reason for s in _sent_reminders(db, pid)] == ["condition"]
    _set_sent_ago(pid, 100, relative_to=_late_night_for() - timedelta(hours=13))   # due before 23:00 and before noon the next day
    # Quiet hours: due, but it's 23:00 for the customer -> waits (no log yet).
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_late_night_for())
        db.commit()
        assert r["sent"] == 0 and outbox.latest_to("slow@example.com") is None
    # Next morning: fires.
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for() + timedelta(days=1))
        db.commit()
        assert r["sent"] == 1
    assert "waiting" in outbox.latest_to("slow@example.com")["subject"]
    # Day 9: the last email step is "not opened" (suppressed -- they did
    # open), and the shop gets its "still waiting" task.
    outbox.clear()
    _set_sent_ago(pid, 24 * 9, relative_to=_noon_for() + timedelta(days=2))
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for() + timedelta(days=2))
        db.commit()
        assert r["sent"] == 1
    assert outbox.latest_to("slow@example.com") is None
    task = outbox.latest_to("dana-chase2@shop.example")
    assert task and "Still waiting" in task["subject"]


def test_snooze_and_opt_out_suppress_customer_reminders(document, outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-chase4@shop.example", outbox)
    pid = create_and_compose(shop, document, email="busy@example.com")
    send_current(shop, pid, outbox, "busy@example.com")
    outbox.clear()
    # Snoozed: the due email step is suppressed for that reason.
    shop.post(f"/proofs/{pid}/snooze", data={"days": "5"}, follow_redirects=False)
    _set_sent_ago(pid, 72)
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for())
        db.commit()
        assert r["sent"] == 0 and [x.suppressed_reason for x in _sent_reminders(db, pid)] == ["snoozed"]
    # Opted out: the next email step is suppressed for that reason.
    with database.SessionLocal() as db:
        p = db.get(Proof, pid)
        p.reminders_snoozed_until = None
        db.get(Contact, p.contact_id).opted_out_at = "2026-01-01T00:00:00Z"
        db.commit()
    _set_sent_ago(pid, 24 * 9, relative_to=_noon_for() + timedelta(days=1))
    with database.SessionLocal() as db:
        r = reminders.run_due(db, now=_noon_for() + timedelta(days=1))
        db.commit()
        reasons = [x.suppressed_reason for x in _sent_reminders(db, pid) if x.status == "suppressed"]
        assert "opted_out" in reasons
    assert outbox.latest_to("busy@example.com") is None


def test_intake_cadence_nudges_for_artwork(outbox):
    shop = TestClient(app)
    sign_in(shop, "dana-chase3@shop.example", outbox)
    r = shop.post("/proofs/new", data={"title": "Logo", "contact_name": "Lee", "contact_email": "lee@example.com"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    shop.post(f"/proofs/{pid}/intake/send", follow_redirects=False)
    outbox.clear()
    with database.SessionLocal() as db:
        reminders.run_due(db, now=_noon_for() + timedelta(days=3))
        db.commit()
        assert len([x for x in _sent_reminders(db, pid) if x.status == "sent"]) == 1
    mail = outbox.latest_to("lee@example.com")
    assert mail and "still need your artwork" in mail["subject"] and "/i/" in mail["text"]
