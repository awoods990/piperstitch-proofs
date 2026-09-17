"""Shop sign-in and roles.

An **owner** signs in with the same email code PiperStitch itself uses:
Proofs asks License Admin to send it (`/api/web/signin/request`), verifies
it (`/api/web/signin/verify`), and reads the account's state and saved
projects with the returned web-session token. The Proofs account is
created on first sign-in, keyed by License Admin's customer id.

An **invited seat user** (sales, stitcher) has no License Admin identity;
Proofs sends its own code. With `REQUIRE_LICENSE_ADMIN_SIGNIN` off
(development, tests) every sign-in uses a Proofs code and the first
sign-in of an unknown email creates an owner account.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, core_client, emailer
from .db import Account, AccountUser, SignInCode, User, WebSession, parse_ts, utcnow


class AuthError(Exception):
    pass


ROLE_CAN = {
    "owner": {"create", "compose", "send", "respond", "on_behalf", "release", "settings", "complete", "void", "invite", "read"},
    "sales": {"create", "compose", "send", "respond", "on_behalf", "void", "read"},
    "stitcher": {"read", "complete"},
}


def can(role: str, action: str) -> bool:
    return action in ROLE_CAN.get(role, set())


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _member_for_email(db: Session, email: str) -> Optional[AccountUser]:
    u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if u is None or u.disabled_at:
        return None
    return db.execute(select(AccountUser).where(AccountUser.user_id == u.id, AccountUser.disabled_at.is_(None))).scalars().first()


def _uses_license_admin(db: Session, email: str) -> bool:
    if not config.REQUIRE_LICENSE_ADMIN_SIGNIN or not core_client.license_admin.configured:
        return False
    m = _member_for_email(db, email)
    # Known seat users (non-owners) get a Proofs code; everyone else is an owner
    # (existing or new) and goes through License Admin.
    return m is None or m.role == "owner"


def request_code(db: Session, email: str) -> str:
    """Returns which path was used: 'license_admin' or 'proofs'."""
    email = email.strip().lower()
    if "@" not in email:
        raise AuthError("Enter a valid email address.")
    if _uses_license_admin(db, email):
        try:
            core_client.license_admin.request_signin(email)
        except core_client.CoreError as e:
            raise AuthError(str(e)) from e
        return "license_admin"
    m = _member_for_email(db, email)
    if m is None and config.REQUIRE_LICENSE_ADMIN_SIGNIN:
        raise AuthError("That email isn't on a PiperStitch account. Sign in with the email you use for PiperStitch.")
    code = f"{secrets.randbelow(10**6):06d}"
    db.add(SignInCode(email=email, code_hash=_hash(code), expires_at=(datetime.now(timezone.utc) + timedelta(minutes=config.SIGNIN_CODE_TTL_MINUTES)).replace(microsecond=0).isoformat().replace("+00:00", "Z")))
    db.flush()
    emailer.send(to_email=email, subject="Your PiperStitch Proofs sign-in code", text=f"Your sign-in code is {code}. It expires in {config.SIGNIN_CODE_TTL_MINUTES} minutes.")
    return "proofs"


def verify_code(db: Session, email: str, code: str) -> tuple[str, AccountUser]:
    """Returns (session token plaintext, membership)."""
    email = email.strip().lower()
    code = code.strip()
    if _uses_license_admin(db, email):
        try:
            core_token = core_client.license_admin.verify_signin(email, code)
            state = core_client.license_admin.session_state(core_token)
        except core_client.CoreError as e:
            raise AuthError(str(e)) from e
        member = _ensure_owner(db, email=state.email or email, name=state.name, core_customer_id=state.customer_id)
        member.core_session_token = core_token
        apply_core_setup(db, member)
    else:
        row = db.execute(select(SignInCode).where(SignInCode.email == email, SignInCode.used_at.is_(None)).order_by(SignInCode.expires_at.desc())).scalars().first()
        if row is None or parse_ts(row.expires_at) < datetime.now(timezone.utc):
            raise AuthError("That code has expired. Request a new one.")
        row.attempts += 1
        if row.attempts > 6 or not hmac.compare_digest(row.code_hash, _hash(code)):
            raise AuthError("That code isn't right.")
        row.used_at = utcnow()
        member = _member_for_email(db, email)
        if member is None:
            member = _ensure_owner(db, email=email, name="", core_customer_id=None)
    if not member.accepted_at:
        member.accepted_at = utcnow()
    member.user.last_seen_at = utcnow()
    plaintext = secrets.token_urlsafe(32)
    db.add(WebSession(token_hash=_hash(plaintext), account_user_id=member.id))
    db.flush()
    return plaintext, member


def sign_in_with_handoff(db: Session, code: str, user_agent: str = "") -> tuple[str, AccountUser]:
    """Arriving from PiperStitch already signed in: the app minted a
    one-time code with License Admin; redeeming it gives this service
    its own PiperStitch session for the same customer. Same account and
    membership as an email-code sign-in would create."""
    try:
        core_token, state = core_client.license_admin.redeem_handoff(code, user_agent=user_agent)
    except core_client.CoreError as e:
        raise AuthError(str(e)) from e
    member = _ensure_owner(db, email=state.email, name=state.name, core_customer_id=state.customer_id)
    member.core_session_token = core_token
    apply_core_setup(db, member)
    if not member.accepted_at:
        member.accepted_at = utcnow()
    member.user.last_seen_at = utcnow()
    plaintext = secrets.token_urlsafe(32)
    db.add(WebSession(token_hash=_hash(plaintext), account_user_id=member.id))
    db.flush()
    return plaintext, member


def core_handoff_url(member: AccountUser, *, path_query: str = "") -> Optional[str]:
    """A link into PiperStitch that signs the owner in on arrival (their
    stored PiperStitch session mints a handoff code). None when this
    member never signed in through PiperStitch -- the caller falls back
    to a plain link."""
    if not member.core_session_token or not core_client.license_admin.configured:
        return None
    try:
        code = core_client.license_admin.create_handoff(member.core_session_token, target="core")
    except core_client.CoreError:
        return None
    sep = "&" if path_query else ""
    return f"{config.CORE_WEB_APP_URL}/?handoff={code}{sep}{path_query}"


def apply_core_setup(db: Session, member: AccountUser) -> bool:
    """Copies the answers from PiperStitch's guided setup (the shop, its
    reply-to address, response window, reminders, release gate, units)
    onto this account. The answers live in the customer's PiperStitch
    preferences (`business`, `proofsDefaults`, `units`, `onboarding`) --
    the same record the thread library comes from. Applied once per
    completed setup run, keyed on `onboarding.completedAt`: settings
    edited here afterwards are left alone, and running guided setup again
    over there applies the new answers. Returns whether anything changed.
    Any failure to reach License Admin is ignored -- the account is
    simply not pre-filled."""
    if not member.core_session_token or not core_client.license_admin.configured:
        return False
    try:
        prefs = core_client.license_admin.get_preferences(member.core_session_token) or {}
    except core_client.CoreError:
        return False
    onboarding = prefs.get("onboarding") or {}
    completed = str(onboarding.get("completedAt") or "")
    if not completed:
        return False
    a = member.account
    if a.core_setup_applied == completed:
        return False
    business = prefs.get("business") or {}
    defaults = prefs.get("proofsDefaults") or {}
    shop_name = str(defaults.get("shopName") or business.get("name") or "").strip()
    if shop_name:
        a.shop_name = shop_name
    reply_to = str(defaults.get("replyTo") or "").strip()
    if reply_to:
        a.reply_to_email = reply_to
    phone = str(business.get("phone") or "").strip()
    if phone:
        a.phone = phone
    if prefs.get("units") in ("cm", "in"):
        a.units = "metric" if prefs["units"] == "cm" else "imperial"
    try:
        a.default_response_window_days = max(1, min(60, int(defaults.get("responseWindowDays") or a.default_response_window_days)))
    except (TypeError, ValueError):
        pass
    if isinstance(defaults.get("remindersEnabled"), bool):
        a.reminders_enabled = defaults["remindersEnabled"]
    if defaults.get("releaseGate") in ("soft", "hard", "off"):
        a.release_gate_policy = defaults["releaseGate"]
    a.core_setup_applied = completed
    db.flush()
    return True


def _ensure_owner(db: Session, *, email: str, name: str, core_customer_id: Optional[int]) -> AccountUser:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, name=name or "")
        db.add(user)
        db.flush()
    elif name and not user.name:
        user.name = name
    member = db.execute(select(AccountUser).where(AccountUser.user_id == user.id, AccountUser.disabled_at.is_(None))).scalars().first()
    if member is not None:
        if core_customer_id and member.account.core_customer_id is None:
            member.account.core_customer_id = core_customer_id
        return member
    account = None
    if core_customer_id is not None:
        account = db.execute(select(Account).where(Account.core_customer_id == core_customer_id)).scalar_one_or_none()
    if account is None:
        account = Account(core_customer_id=core_customer_id, shop_name="", reply_to_email=email)
        db.add(account)
        db.flush()
    member = AccountUser(account_id=account.id, user_id=user.id, role="owner", accepted_at=utcnow())
    db.add(member)
    db.flush()
    from . import reminders
    reminders.seed_defaults(db, account)
    return member


def session_member(db: Session, token: Optional[str]) -> Optional[AccountUser]:
    if not token:
        return None
    s = db.execute(select(WebSession).where(WebSession.token_hash == _hash(token), WebSession.revoked_at.is_(None))).scalar_one_or_none()
    if s is None:
        return None
    s.last_seen_at = utcnow()
    m = s.account_user
    if m.disabled_at or m.user.disabled_at:
        return None
    return m


def sign_out(db: Session, token: Optional[str]) -> None:
    if not token:
        return
    s = db.execute(select(WebSession).where(WebSession.token_hash == _hash(token))).scalar_one_or_none()
    if s:
        s.revoked_at = utcnow()


def invite(db: Session, account: Account, *, email: str, role: str, name: str = "") -> AccountUser:
    email = email.strip().lower()
    if role not in ("sales", "stitcher", "owner"):
        raise AuthError("Role must be owner, sales or stitcher.")
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, name=name)
        db.add(user)
        db.flush()
    existing = db.execute(select(AccountUser).where(AccountUser.user_id == user.id, AccountUser.account_id == account.id)).scalar_one_or_none()
    if existing:
        existing.role = role
        existing.disabled_at = None
        return existing
    m = AccountUser(account_id=account.id, user_id=user.id, role=role)
    db.add(m)
    db.flush()
    emailer.send(to_email=email, subject=f"You've been added to {account.shop_name or 'a PiperStitch Proofs account'}",
                 text=f"Sign in at {config.PUBLIC_BASE_URL}/signin with this email to see the proofs board (role: {role}).")
    return m
