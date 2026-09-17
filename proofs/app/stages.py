"""Where a proof stands, in the shop's terms -- the one model the
dashboard, the proof header and the "what's next" callout all read.

Six steps that every job walks through:
  Artwork → Digitized → Proof sent → Approved → Released → Sewn
plus who the job is waiting on right now and the single next action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db import Proof, ProofVersion, parse_ts

STEPS = ("Artwork", "Digitized", "Proof sent", "Approved", "Released", "Sewn")

# The mini-steps of building a proof, shown under the tracker between
# "Digitized" and "Proof sent" while that's where the job is.
BUILD_STEPS = ("Design", "Garment & placement", "Your note", "Review & send")


def build_states(*, design_linked: bool, composing: bool, composed_unsent: bool) -> list[str]:
    """Per BUILD_STEPS: done | current | todo. On the compose page the form
    covers steps 1-3 at once (design is done when a project is linked);
    on the proof page with a built, unsent version only "Review & send"
    is left."""
    if composed_unsent:
        return ["done", "done", "done", "current"]
    if composing:
        return ["done" if design_linked else "current", "current" if design_linked else "todo", "current" if design_linked else "todo", "todo"]
    return ["done" if design_linked else "todo", "todo", "todo", "todo"]


@dataclass
class Stage:
    step: int                      # 0..6 = how many steps are complete
    waiting_on: str                # you | customer | colleague | nobody
    headline: str                  # one line: what's going on
    next_action: str               # the button's label, or ""
    next_hint: str = ""            # one sentence under the button
    next_kind: str = ""            # link | send | resend | compose | release | complete | review | none
    days_waiting: int = 0
    overdue: bool = False
    tone: str = "neutral"          # neutral | good | warn | bad
    step_states: list[str] = field(default_factory=list)   # per step: done | current | todo | stalled


def _days_since(iso: Optional[str]) -> int:
    ts = parse_ts(iso) if iso else None
    if not ts:
        return 0
    return max(0, int((datetime.now(timezone.utc) - ts).total_seconds() // 86400))


def stage_for(db: Session, proof: Proof, *, has_blockers: bool = False) -> Stage:
    v = db.get(ProofVersion, proof.current_version_id) if proof.current_version_id else None
    s = proof.status
    st = Stage(step=0, waiting_on="you", headline="", next_action="")

    if s == "void":
        st = Stage(step=0, waiting_on="nobody", headline="Cancelled" + (f" — {proof.void_reason}" if proof.void_reason else ""), next_action="", tone="bad")
    elif s == "completed":
        st = Stage(step=6, waiting_on="nobody", headline="Sewn and closed", next_action="", tone="good")
    elif s == "released":
        st = Stage(step=5, waiting_on="you", headline="Released to production — print the production sheet", next_action="Print the production sheet", next_kind="sheet",
                   next_hint="One page for the machine: hoop, backing, needle, size, placement, the approved thread colours in order, and a checklist. Mark the job sewn when the run is done.",
                   days_waiting=_days_since(proof.released_at), tone="good")
    elif s in ("approved", "approved_with_notes"):
        st = Stage(step=4, waiting_on="you", headline="Approved by the customer" + (" (with notes)" if s == "approved_with_notes" else ""),
                   next_action="Release to production", next_kind="release",
                   next_hint="Verifies the approved files are untouched and unlocks the run ticket for your stitcher.", days_waiting=_days_since(proof.updated_at), tone="good")
    elif s in ("sent", "viewed"):
        days = _days_since(v.sent_at if v else None)
        overdue = bool(v and v.response_expires_at and parse_ts(v.response_expires_at) < datetime.now(timezone.utc))
        st = Stage(step=3, waiting_on="customer", headline=("Opened, no answer yet" if s == "viewed" else "Sent, not opened yet") + f" — {days} day{'s' if days != 1 else ''}",
                   next_action="", next_kind="none", next_hint="Reminders go out automatically; snooze them if you've spoken to the customer.",
                   days_waiting=days, overdue=overdue, tone="warn" if days >= 4 else "neutral")
    elif s == "changes_requested":
        st = Stage(step=3, waiting_on="you", headline="Customer asked for changes", next_action=f"Build version {(v.version_number + 1) if v else 2}", next_kind="compose",
                   next_hint="Work through their list in PiperStitch, then build and send the next version. They'll see what changed.", days_waiting=_days_since(proof.updated_at), tone="warn")
    elif s == "expired":
        st = Stage(step=3, waiting_on="you", headline="No answer before the deadline", next_action="Send a fresh link", next_kind="resend",
                   next_hint="Starts a new response window with a new link; the old one stops working.", days_waiting=_days_since(proof.updated_at), tone="warn")
    elif s == "declined":
        st = Stage(step=3, waiting_on="you", headline="Customer declined this proof", next_action="Send a fresh link", next_kind="resend",
                   next_hint="If they've changed their mind; otherwise cancel the proof.", tone="bad")
    elif s == "internal_review":
        st = Stage(step=2, waiting_on="colleague", headline="Waiting for a colleague to check it", next_action="", next_kind="review",
                   next_hint="Whoever reviews it can pass it or send it back with a note.", days_waiting=_days_since(proof.updated_at))
    elif s == "ready_to_send":
        st = Stage(step=2, waiting_on="you", headline="Proof built — check it, then send", next_action="Send proof", next_kind="send",
                   next_hint="Look over the render below first. Need to tweak the stitching? Adjust it in PiperStitch, save, and build again. Sending emails (and texts, if opted in) a private link.")
    elif s == "awaiting_art":
        days = _days_since(proof.updated_at)
        st = Stage(step=0, waiting_on="customer", headline=f"Waiting for the customer's artwork — {days} day{'s' if days != 1 else ''}", next_action="", next_kind="none",
                   next_hint="They have a link to upload it; reminders go out automatically.", days_waiting=days, tone="warn" if days >= 3 else "neutral")
    elif s == "intake_expired":
        st = Stage(step=0, waiting_on="you", headline="Artwork link expired with nothing uploaded", next_action="Resend the artwork request", next_kind="intake",
                   next_hint="Sends a new link with a fresh deadline.", tone="warn")
    elif s in ("art_received", "digitizing", "draft"):
        if has_blockers:
            st = Stage(step=1, waiting_on="you", headline="Artwork received, but the Readiness Report found a blocker", next_action="", next_kind="triage",
                       next_hint="Resolve or override it below before building the proof.", tone="warn")
        elif proof.core_project_id:
            st = Stage(step=2, waiting_on="you", headline="Digitized in PiperStitch — ready to build the proof", next_action="Build the proof", next_kind="compose",
                       next_hint="Renders the stitches, the garment mockup and the PDF from the digitized design.")
        elif s == "art_received":
            st = Stage(step=1, waiting_on="you", headline="Artwork received — digitize it next", next_action="Open PiperStitch", next_kind="core",
                       next_hint="Import the artwork in PiperStitch, save it as a project, then build the proof from it.")
        else:
            st = Stage(step=0, waiting_on="you", headline="New — no artwork yet", next_action="Ask the customer for artwork", next_kind="intake",
                       next_hint="Or upload it yourself if you already have it.")
    # Per-step states for the tracker.
    states = []
    for i in range(6):
        if i < st.step:
            states.append("done")
        elif i == st.step and s not in ("void", "completed"):
            states.append("stalled" if st.tone in ("warn", "bad") and st.waiting_on != "customer" else "current")
        else:
            states.append("todo")
    st.step_states = states
    return st
