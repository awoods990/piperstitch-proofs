"""Access tokens for the public surface -- PRD "Approval and evidence".
The plaintext is only ever in the link; the database holds its SHA-256.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import AccessToken, parse_ts, utcnow


def token_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def mint(db: Session, *, account_id: str, proof_id: str, contact_id: str, purpose: str,
         proof_version_id: Optional[str] = None, approval_record_id: Optional[str] = None,
         days: Optional[int] = None, version_scope: str = "current") -> tuple[str, AccessToken]:
    plaintext = secrets.token_urlsafe(32)
    expires = None
    if purpose != "certificate":
        d = days if days is not None else config.PROOF_TOKEN_DAYS
        from datetime import datetime, timezone
        expires = (datetime.now(timezone.utc) + timedelta(days=d)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    t = AccessToken(account_id=account_id, proof_id=proof_id, proof_version_id=proof_version_id, approval_record_id=approval_record_id,
                    contact_id=contact_id, token_hash=token_hash(plaintext), purpose=purpose, version_scope=version_scope, expires_at=expires)
    db.add(t)
    db.flush()
    return plaintext, t


class TokenState:
    def __init__(self, token: Optional[AccessToken], reason: str):
        self.token = token
        self.reason = reason  # ok | unknown | revoked | expired

    @property
    def ok(self) -> bool:
        return self.reason == "ok"


def resolve(db: Session, plaintext: str, purpose: Optional[str] = None) -> TokenState:
    t = db.execute(select(AccessToken).where(AccessToken.token_hash == token_hash(plaintext))).scalar_one_or_none()
    if t is None or (purpose and t.purpose != purpose):
        return TokenState(None, "unknown")
    if t.revoked_at:
        return TokenState(t, "revoked")
    if t.expires_at:
        from datetime import datetime, timezone
        if parse_ts(t.expires_at) < datetime.now(timezone.utc):
            return TokenState(t, "expired")
    t.last_used_at = utcnow()
    return TokenState(t, "ok")


def revoke_for_version(db: Session, proof_version_id: str, purposes: tuple[str, ...] = ("proof", "reconfirm")) -> int:
    n = 0
    for t in db.execute(select(AccessToken).where(AccessToken.proof_version_id == proof_version_id, AccessToken.revoked_at.is_(None))).scalars():
        if t.purpose in purposes:
            t.revoked_at = utcnow()
            n += 1
    return n


def revoke_for_proof(db: Session, proof_id: str, purposes: tuple[str, ...] = ("proof", "reconfirm", "intake")) -> int:
    n = 0
    for t in db.execute(select(AccessToken).where(AccessToken.proof_id == proof_id, AccessToken.revoked_at.is_(None))).scalars():
        if t.purpose in purposes:
            t.revoked_at = utcnow()
            n += 1
    return n


def extend_on_activity(t: AccessToken, days: int = 7) -> None:
    """A customer who is active keeps their link (PRD: 'extended on activity')."""
    if not t.expires_at:
        return
    from datetime import datetime, timezone
    floor = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)
    if parse_ts(t.expires_at) < floor:
        t.expires_at = floor.isoformat().replace("+00:00", "Z")
