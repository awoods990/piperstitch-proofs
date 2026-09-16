"""The chase engine -- PRD 5.7. Two cadences (intake, response), seeded
per account from defaults and overridable per proof. A step fires when
its offset from the send has elapsed and its condition still holds;
every send or suppression is logged. Rules, all enforced here:

* quiet hours in the contact's timezone: nothing between 20:00 and 08:00
  local (the account's setting); a due step waits for the morning
* one reminder per proof per day, across channels
* suppressed for an opted-out contact, a snoozed proof, or a proof whose
  customer has already acted (the condition check *is* the cancel)
* the shop gets a task-style email at the last step
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, emailer, events
from .db import Account, Contact, Proof, ProofVersion, ReminderSchedule, ReminderSend, parse_ts, utcnow

DEFAULT_CADENCES = {
    # (step, offset_hours, channel, condition)
    "response": [(0, 48, "email", "not_opened"), (1, 96, "email", "opened_no_response"), (2, 96, "email", "not_opened"), (3, 144, "task", "always")],
    "intake": [(0, 48, "email", "always"), (1, 120, "email", "always"), (2, 168, "task", "always")],
}


def seed_defaults(db: Session, account: Account) -> None:
    existing = db.execute(select(ReminderSchedule).where(ReminderSchedule.account_id == account.id, ReminderSchedule.proof_id.is_(None))).scalars().first()
    if existing:
        return
    for cadence, steps in DEFAULT_CADENCES.items():
        for step, hours, channel, condition in steps:
            db.add(ReminderSchedule(account_id=account.id, cadence=cadence, step_index=step, offset_hours=hours, channel=channel, condition=condition))
    db.flush()


def schedule_for(db: Session, proof: Proof, cadence: str) -> list[ReminderSchedule]:
    own = list(db.execute(select(ReminderSchedule).where(ReminderSchedule.proof_id == proof.id, ReminderSchedule.cadence == cadence, ReminderSchedule.enabled.is_(True)).order_by(ReminderSchedule.step_index)).scalars())
    if own:
        return own
    return list(db.execute(select(ReminderSchedule).where(ReminderSchedule.account_id == proof.account_id, ReminderSchedule.proof_id.is_(None),
                                                          ReminderSchedule.cadence == cadence, ReminderSchedule.enabled.is_(True)).order_by(ReminderSchedule.step_index)).scalars())


def _local_hour(now: datetime, tz_name: str) -> int:
    try:
        return now.astimezone(ZoneInfo(tz_name or "America/New_York")).hour
    except Exception:  # noqa: BLE001
        return now.astimezone(ZoneInfo("America/New_York")).hour


def in_quiet_hours(account: Account, contact: Contact, now: datetime) -> bool:
    h = _local_hour(now, contact.timezone)
    start, end = account.quiet_hours_start, account.quiet_hours_end
    if start == end:
        return False
    return h >= start or h < end if start > end else start <= h < end


def _already_today(db: Session, proof_id: str, now: datetime) -> bool:
    day = now.strftime("%Y-%m-%d")
    for s in db.execute(select(ReminderSend).where(ReminderSend.proof_id == proof_id, ReminderSend.status == "sent")).scalars():
        if (s.sent_at or "").startswith(day):
            return True
    return False


def _fired(db: Session, proof_id: str, schedule_id: str, anchor_id: str) -> bool:
    return db.execute(select(ReminderSend).where(ReminderSend.proof_id == proof_id, ReminderSend.reminder_schedule_id == schedule_id,
                                                 ReminderSend.proof_version_id == anchor_id, ReminderSend.status.in_(("sent", "suppressed")))).scalars().first() is not None


def run_due(db: Session, now: Optional[datetime] = None, *, base_url: str = "") -> dict:
    """The scheduler tick. Returns counts."""
    now = now or datetime.now(timezone.utc)
    out = {"sent": 0, "suppressed": 0}
    # Response cadence: every live, unanswered version.
    for v in db.execute(select(ProofVersion).where(ProofVersion.status.in_(("sent", "viewed")))).scalars():
        proof = db.get(Proof, v.proof_id)
        if proof.status in ("void", "completed", "released") or not v.sent_at:
            continue
        out_c = _run_cadence(db, proof, "response", anchor_at=parse_ts(v.sent_at), anchor_id=v.id, opened=(v.status == "viewed"), now=now, base_url=base_url)
        out["sent"] += out_c[0]
        out["suppressed"] += out_c[1]
    # Intake cadence: proofs still waiting for artwork.
    for proof in db.execute(select(Proof).where(Proof.status == "awaiting_art")).scalars():
        sent_event = next((e for e in reversed(events.chain(db, proof.id)) if e.event_type in ("intake_sent",)), None)
        if sent_event is None:
            continue
        out_c = _run_cadence(db, proof, "intake", anchor_at=parse_ts(sent_event.occurred_at), anchor_id="intake:" + sent_event.event_hash[:12], opened=False, now=now, base_url=base_url)
        out["sent"] += out_c[0]
        out["suppressed"] += out_c[1]
    return out


def _run_cadence(db: Session, proof: Proof, cadence: str, *, anchor_at: datetime, anchor_id: str, opened: bool, now: datetime, base_url: str) -> tuple[int, int]:
    account = db.get(Account, proof.account_id)
    contact = db.get(Contact, proof.contact_id)
    if not account.reminders_enabled:
        return 0, 0
    sent = suppressed = 0
    for step in schedule_for(db, proof, cadence):
        due_at = anchor_at + timedelta(hours=step.offset_hours)
        if now < due_at or _fired(db, proof.id, step.id, anchor_id):
            continue
        # Condition = the cancel: a customer who opened doesn't get "you haven't opened".
        if step.condition == "not_opened" and opened:
            _log(db, step, proof, anchor_id, "suppressed", "condition", due_at, now)
            suppressed += 1
            continue
        if step.condition == "opened_no_response" and not opened:
            continue   # not yet applicable; may become so before the next step
        reason = ""
        if step.channel != "task":
            if contact.opted_out_at:
                reason = "opted_out"
            elif proof.reminders_snoozed_until and parse_ts(proof.reminders_snoozed_until) > now:
                reason = "snoozed"
            elif _already_today(db, proof.id, now):
                reason = "one_per_day"
            elif in_quiet_hours(account, contact, now):
                continue   # wait; try again next tick
            elif step.channel == "sms":
                reason = "sms_not_enabled"
        if reason:
            _log(db, step, proof, anchor_id, "suppressed", reason, due_at, now)
            suppressed += 1
            continue
        ok = _deliver(db, step, proof, account, contact, cadence, base_url)
        _log(db, step, proof, anchor_id, "sent" if ok else "failed", "" if ok else "delivery", due_at, now)
        sent += 1 if ok else 0
    return sent, suppressed


def _deliver(db: Session, step: ReminderSchedule, proof: Proof, account: Account, contact: Contact, cadence: str, base_url: str) -> bool:
    shop = account.shop_name or "Your embroiderer"
    from . import tokens as _tokens
    if step.channel == "task":
        if not account.reply_to_email:
            return False
        return emailer.send(to_email=account.reply_to_email, subject=f"Still waiting: {proof.reference} {proof.title}",
                            text=f"{contact.display_name or contact.email} hasn't {'sent artwork' if cadence == 'intake' else 'answered the proof'} after the reminders. Time for a call?\n\n{base_url or config.PUBLIC_BASE_URL}/proofs/{proof.id}")
    if not contact.email:
        return False
    purpose = "intake" if cadence == "intake" else "proof"
    live = db.execute(select(_tokens.AccessToken).where(_tokens.AccessToken.proof_id == proof.id, _tokens.AccessToken.purpose == purpose,
                                                        _tokens.AccessToken.revoked_at.is_(None)).order_by(_tokens.AccessToken.issued_at.desc())).scalars().first()
    # The plaintext isn't stored; a reminder carries a fresh link and
    # revokes the old one (same customer, same version).
    version_id = live.proof_version_id if live else proof.current_version_id
    if live:
        live.revoked_at = utcnow()
    plaintext, _t = _tokens.mint(db, account_id=account.id, proof_id=proof.id, contact_id=contact.id, purpose=purpose, proof_version_id=version_id)
    if cadence == "intake":
        url = f"{base_url or config.PUBLIC_BASE_URL}/i/{plaintext}"
        text = f"Hi {contact.display_name or 'there'},\n\nJust a nudge — {shop} is waiting on your artwork for {proof.title} before work can start.\n\nUpload here:\n{url}\n\n{shop}"
        subject = f"{shop}: still need your artwork for {proof.title}"
    else:
        url = f"{base_url or config.PUBLIC_BASE_URL}/p/{plaintext}"
        text = f"Hi {contact.display_name or 'there'},\n\nYour proof for {proof.title} is waiting for a yes (or changes). It takes a minute on your phone:\n{url}\n\nNothing gets sewn until you approve.\n\n{shop}"
        subject = f"{shop}: your proof {proof.reference} is waiting"
    ok = emailer.send(to_email=contact.email, subject=subject, text=text, reply_to=account.reply_to_email)
    events.append(db, proof_id=proof.id, proof_version_id=version_id if cadence != "intake" else None, event_type="reminder_sent", actor_type="system",
                  payload={"cadence": cadence, "step": step.step_index, "channel": step.channel, "ok": ok})
    return ok


def _log(db: Session, step: ReminderSchedule, proof: Proof, anchor_id: str, status: str, reason: str, due_at: datetime, now: datetime) -> None:
    contact = db.get(Contact, proof.contact_id)
    db.add(ReminderSend(reminder_schedule_id=step.id, proof_id=proof.id, proof_version_id=anchor_id, channel=step.channel, to_address=contact.email,
                        status=status, suppressed_reason=reason, scheduled_for=due_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        sent_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z") if status == "sent" else None))
    db.flush()


def snooze(proof: Proof, days: int) -> None:
    proof.reminders_snoozed_until = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
