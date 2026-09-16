"""The evidence chain -- PRD principle 5. Every state change is a
`proof_event`, appended with a hash over its own content plus the hash
of the previous event for the same proof. `verify_chain` recomputes it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import ProofEvent, utcnow


def _event_hash(prev_hash: str, fields: dict) -> str:
    material = prev_hash + "\n" + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _fields(e: ProofEvent) -> dict:
    return {
        "proof_id": e.proof_id, "proof_version_id": e.proof_version_id, "sequence": e.sequence,
        "event_type": e.event_type, "occurred_at": e.occurred_at, "local_offset": e.local_offset,
        "time_source": e.time_source, "actor_type": e.actor_type, "actor_id": e.actor_id,
        "token_hash": e.token_hash, "ip": e.ip, "user_agent_raw": e.user_agent_raw, "payload": json.loads(e.payload_json or "{}"),
    }


def head(db: Session, proof_id: str) -> Optional[ProofEvent]:
    return db.execute(select(ProofEvent).where(ProofEvent.proof_id == proof_id).order_by(ProofEvent.sequence.desc()).limit(1)).scalar_one_or_none()


def append(db: Session, *, proof_id: str, event_type: str, actor_type: str, actor_id: str = "",
           proof_version_id: Optional[str] = None, token_hash: str = "", ip: str = "", user_agent: str = "",
           payload: Optional[dict] = None, local_offset: str = "") -> ProofEvent:
    last = head(db, proof_id)
    e = ProofEvent(
        proof_id=proof_id, proof_version_id=proof_version_id, sequence=(last.sequence + 1) if last else 1,
        event_type=event_type, occurred_at=utcnow(), local_offset=local_offset, time_source="server",
        actor_type=actor_type, actor_id=actor_id, token_hash=token_hash, ip=ip or "", user_agent_raw=user_agent or "",
        payload_json=json.dumps(payload or {}, sort_keys=True, ensure_ascii=False),
        prev_event_hash=last.event_hash if last else "",
    )
    e.event_hash = _event_hash(e.prev_event_hash, _fields(e))
    db.add(e)
    db.flush()
    return e


def chain(db: Session, proof_id: str) -> list[ProofEvent]:
    return list(db.execute(select(ProofEvent).where(ProofEvent.proof_id == proof_id).order_by(ProofEvent.sequence)).scalars())


def verify_chain(db: Session, proof_id: str) -> tuple[bool, str]:
    prev = ""
    expected_seq = 1
    for e in chain(db, proof_id):
        if e.sequence != expected_seq:
            return False, f"sequence gap at {e.sequence}"
        if e.prev_event_hash != prev:
            return False, f"broken link at sequence {e.sequence}"
        if _event_hash(prev, _fields(e)) != e.event_hash:
            return False, f"hash mismatch at sequence {e.sequence}"
        prev = e.event_hash
        expected_seq += 1
    return True, "ok"
