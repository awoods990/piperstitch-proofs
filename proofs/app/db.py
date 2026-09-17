"""The data model -- PRD "Data model", Phase 1 subset, as SQLAlchemy so the
same code runs on SQLite today and Postgres later (PRD v1.1 change 2).

Every table carries account_id; deletion is `archived_at`; timestamps are
ISO-8601 UTC strings (SQLite has no timestamptz -- we store text and parse
on read, which round-trips exactly and sorts correctly).

`proof_event` is append-only. On SQLite that is enforced by triggers that
abort any UPDATE or DELETE (see `_APPEND_ONLY_TRIGGERS`); on Postgres the
equivalent is a table grant with no UPDATE/DELETE. Either way the hash
chain and the certificate carry the evidence independently.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, select, text)  # noqa: F401  (select/text re-exported for callers)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import config


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id() -> str:
    return secrets.token_hex(12)


class Base(DeclarativeBase):
    pass


# --- identity and access -------------------------------------------------------

class Account(Base):
    """One embroidery shop. `core_customer_id` is License Admin's customer
    id -- the account exists in Proofs because that customer signed in."""
    __tablename__ = "account"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    core_customer_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, nullable=True)
    shop_name: Mapped[str] = mapped_column(String, default="")
    # art@{slug}.piperstitch.com -- unique, immutable once issued.
    slug: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    reply_to_email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    brand_color: Mapped[str] = mapped_column(String, default="#2c6e8f")
    logo_key: Mapped[str] = mapped_column(String, default="")      # storage key of the shop's logo (PNG/JPEG), "" = none
    logo_mime: Mapped[str] = mapped_column(String, default="")
    release_gate_policy: Mapped[str] = mapped_column(String, default="soft")  # hard | soft | off
    units: Mapped[str] = mapped_column(String, default="imperial")
    default_response_window_days: Mapped[int] = mapped_column(Integer, default=config.DEFAULT_RESPONSE_WINDOW_DAYS)
    default_revisions_included: Mapped[int] = mapped_column(Integer, default=2)
    quiet_hours_start: Mapped[int] = mapped_column(Integer, default=20)   # local hour, inclusive
    quiet_hours_end: Mapped[int] = mapped_column(Integer, default=8)      # local hour, exclusive
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_reference: Mapped[int] = mapped_column(Integer, default=1000)
    # The `completedAt` of the PiperStitch guided setup whose answers were
    # last copied onto this account (auth.apply_core_setup) -- so one setup
    # run is applied once, and settings edited here afterwards stand.
    core_setup_applied: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    archived_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    users: Mapped[list["AccountUser"]] = relationship(back_populates="account")
    entitlements: Mapped[Optional["AccountEntitlements"]] = relationship(back_populates="account", uselist=False)


class User(Base):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, default="")
    last_seen_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    disabled_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


ROLES = ("owner", "sales", "stitcher")


class AccountUser(Base):
    __tablename__ = "account_user"
    __table_args__ = (UniqueConstraint("account_id", "user_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    role: Mapped[str] = mapped_column(String, default="owner")
    invited_at: Mapped[str] = mapped_column(String, default=utcnow)
    accepted_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    disabled_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    counts_against_seats: Mapped[bool] = mapped_column(Boolean, default=True)
    # The License Admin web-session token for an owner, so Proofs can list
    # and fetch their saved Core projects on their behalf.
    core_session_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    account: Mapped[Account] = relationship(back_populates="users")
    user: Mapped[User] = relationship()


class AccountEntitlements(Base):
    __tablename__ = "account_entitlements"
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), primary_key=True)
    plan_code: Mapped[str] = mapped_column(String, default="free")
    proofs_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    clients_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    seats: Mapped[int] = mapped_column(Integer, default=1)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    free_proofs_granted: Mapped[int] = mapped_column(Integer, default=config.FREE_PROOFS_GRANTED)
    free_proofs_used: Mapped[int] = mapped_column(Integer, default=0)
    trial_ends_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subscription_status: Mapped[str] = mapped_column(String, default="")
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    # When the shop signed in through PiperStitch, License Admin owns the
    # plan and these columns mirror it (proofs.entitlements refreshes them).
    managed_by: Mapped[str] = mapped_column(String, default="")          # "" (this service's own Stripe) | "license_admin"
    has_billing: Mapped[bool] = mapped_column(Boolean, default=False)    # a Stripe customer exists in License Admin, so the portal works
    price_cents: Mapped[int] = mapped_column(Integer, default=0)          # the Proofs price License Admin quoted; 0 = use PROOFS_PRICE_CENTS
    synced_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    account: Mapped[Account] = relationship(back_populates="entitlements")

    @property
    def can_send(self) -> bool:
        return self.proofs_enabled or self.free_proofs_used < self.free_proofs_granted


class StripeEvent(Base):
    """Webhook idempotency: one row per Stripe event id we've applied."""
    __tablename__ = "stripe_event"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[str] = mapped_column(String, default=utcnow)


class SignInCode(Base):
    """A Proofs-issued email code (invited seat users; owners in dev)."""
    __tablename__ = "signin_code"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String, index=True)
    code_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    used_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class WebSession(Base):
    __tablename__ = "web_session"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    account_user_id: Mapped[str] = mapped_column(ForeignKey("account_user.id"))
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    last_seen_at: Mapped[str] = mapped_column(String, default=utcnow)
    revoked_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    account_user: Mapped[AccountUser] = relationship()


# --- the thin shared party record ------------------------------------------------

class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    company_name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    preferred_channel: Mapped[str] = mapped_column(String, default="email")
    timezone: Mapped[str] = mapped_column(String, default="America/New_York")
    sms_opt_in_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sms_opt_in_text: Mapped[str] = mapped_column(Text, default="")
    opted_out_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    archived_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


# --- core entities ------------------------------------------------------------------

class Proof(Base):
    __tablename__ = "proof"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contact.id"))
    reference: Mapped[str] = mapped_column(String)          # PF-1042
    title: Mapped[str] = mapped_column(String, default="")
    # Derived rollup -- see `proofs.rollup_status`; never hand-set.
    status: Mapped[str] = mapped_column(String, default="draft")
    current_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    core_project_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    core_project_name: Mapped[str] = mapped_column(String, default="")
    due_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    intake_window_days: Mapped[int] = mapped_column(Integer, default=7)
    intake_expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    requested_width_mm: Mapped[float] = mapped_column(Float, default=0)
    triage_override_reason: Mapped[str] = mapped_column(Text, default="")
    response_window_days: Mapped[int] = mapped_column(Integer, default=config.DEFAULT_RESPONSE_WINDOW_DAYS)
    revisions_included: Mapped[int] = mapped_column(Integer, default=2)
    reminders_snoozed_until: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    void_reason: Mapped[str] = mapped_column(Text, default="")
    released_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow)
    archived_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    contact: Mapped[Contact] = relationship()
    versions: Mapped[list["ProofVersion"]] = relationship(back_populates="proof", order_by="ProofVersion.version_number")


class ProofVersion(Base):
    """Immutable once sent. Everything the customer consented to is
    copied here at composition; the Core project can change freely."""
    __tablename__ = "proof_version"
    __table_args__ = (UniqueConstraint("proof_id", "version_number"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="ready_to_send")
    design_hash: Mapped[str] = mapped_column(String, default="")
    stitch_count: Mapped[int] = mapped_column(Integer, default=0)
    color_change_count: Mapped[int] = mapped_column(Integer, default=0)
    trim_count: Mapped[int] = mapped_column(Integer, default=0)
    width_mm: Mapped[float] = mapped_column(Float, default=0)
    height_mm: Mapped[float] = mapped_column(Float, default=0)
    estimated_run_seconds: Mapped[float] = mapped_column(Float, default=0)
    fabric_code: Mapped[str] = mapped_column(String, default="standard")
    hoop_code: Mapped[str] = mapped_column(String, default="")
    stabilizer_advice: Mapped[str] = mapped_column(Text, default="")
    garment_style_name: Mapped[str] = mapped_column(String, default="")
    garment_color: Mapped[str] = mapped_column(String, default="")
    garment_template_id: Mapped[str] = mapped_column(String, default="")
    garment_zone: Mapped[str] = mapped_column(String, default="")
    placement_name: Mapped[str] = mapped_column(String, default="")
    placement_notes: Mapped[str] = mapped_column(Text, default="")
    placement_down_mm: Mapped[float] = mapped_column(Float, default=0)
    placement_across_mm: Mapped[float] = mapped_column(Float, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    size_breakdown_json: Mapped[str] = mapped_column(Text, default="{}")
    price_line: Mapped[str] = mapped_column(String, default="")
    terms_version_id: Mapped[Optional[str]] = mapped_column(ForeignKey("terms_version.id"), nullable=True)
    message_body: Mapped[str] = mapped_column(Text, default="")
    # The Core document as composed -- the source of the render and of
    # every machine file, kept so a release can regenerate byte-identical
    # files and so the hashes can be re-verified.
    document_json: Mapped[str] = mapped_column(Text, default="")
    artifact_hashes_json: Mapped[str] = mapped_column(Text, default="{}")
    artifact_rev: Mapped[int] = mapped_column(Integer, default=0)   # bumped when an unsent version's placement is moved; see proofs.artifact_key
    composed_by: Mapped[Optional[str]] = mapped_column(ForeignKey("user.id"), nullable=True)
    composed_at: Mapped[str] = mapped_column(String, default=utcnow)
    sent_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    response_expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    superseded_by_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    proof: Mapped[Proof] = relationship(back_populates="versions")
    thread_stops: Mapped[list["ThreadStop"]] = relationship(back_populates="version", order_by="ThreadStop.stop_number",
                                                           primaryjoin="and_(ProofVersion.id == ThreadStop.proof_version_id, ThreadStop.colorway_id.is_(None))")
    colorways: Mapped[list["Colorway"]] = relationship(order_by="Colorway.ordinal")
    terms_version: Mapped[Optional["TermsVersion"]] = relationship()

    @property
    def artifact_hashes(self) -> dict:
        return json.loads(self.artifact_hashes_json or "{}")

    @property
    def size_breakdown(self) -> dict:
        return json.loads(self.size_breakdown_json or "{}")

    @property
    def is_live(self) -> bool:
        return self.status in ("sent", "viewed", "changes_requested", "approved", "approved_with_notes")


class Colorway(Base):
    """An alternate thread assignment for a version (PRD: up to four,
    the customer picks one as part of approval). Ordinal 1 is the
    version's own stops; 2-4 carry their own `ThreadStop` rows."""
    __tablename__ = "colorway"
    __table_args__ = (UniqueConstraint("proof_version_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_version_id: Mapped[str] = mapped_column(ForeignKey("proof_version.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)

    stops: Mapped[list["ThreadStop"]] = relationship(primaryjoin="Colorway.id == foreign(ThreadStop.colorway_id)", order_by="ThreadStop.stop_number", viewonly=True)


class ThreadStop(Base):
    __tablename__ = "thread_stop"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_version_id: Mapped[str] = mapped_column(ForeignKey("proof_version.id"), index=True)
    colorway_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)   # NULL = the version's own (colorway 1)
    stop_number: Mapped[int] = mapped_column(Integer)
    thread_brand: Mapped[str] = mapped_column(String, default="")
    thread_code: Mapped[str] = mapped_column(String, default="")
    thread_name: Mapped[str] = mapped_column(String, default="")
    hex: Mapped[str] = mapped_column(String, default="#000000")
    stitch_count: Mapped[int] = mapped_column(Integer, default=0)

    version: Mapped[ProofVersion] = relationship(back_populates="thread_stops")


class TermsVersion(Base):
    """Never edited; a change creates a new row and becomes current."""
    __tablename__ = "terms_version"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    label: Mapped[str] = mapped_column(String, default="v1")
    body: Mapped[str] = mapped_column(Text, default="")
    consent_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)


# --- intake and triage --------------------------------------------------------------

class File(Base):
    """Every uploaded or generated binary (PRD `file`)."""
    __tablename__ = "file"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    proof_id: Mapped[Optional[str]] = mapped_column(ForeignKey("proof.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, default="artwork")   # artwork | sew_out_photo | change_attachment | message_attachment | garment_photo | certificate
    source: Mapped[str] = mapped_column(String, default="upload")  # intake | email | upload | mms | generated
    original_filename: Mapped[str] = mapped_column(String, default="")
    mime_type: Mapped[str] = mapped_column(String, default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_key: Mapped[str] = mapped_column(String, default="")
    sha256: Mapped[str] = mapped_column(String, default="")
    scan_status: Mapped[str] = mapped_column(String, default="skipped")  # pending | clean | infected | skipped
    scan_result: Mapped[str] = mapped_column(String, default="")
    uploaded_at: Mapped[str] = mapped_column(String, default=utcnow)
    uploaded_by_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class TriageReport(Base):
    __tablename__ = "triage_report"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(ForeignKey("file.id"), index=True)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    kind: Mapped[str] = mapped_column(String, default="")
    effective_ppi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_vector: Mapped[bool] = mapped_column(Boolean, default=False)
    has_transparency: Mapped[bool] = mapped_column(Boolean, default=False)
    color_count: Mapped[int] = mapped_column(Integer, default=0)
    colorspace: Mapped[str] = mapped_column(String, default="")
    min_feature_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    requested_width_mm: Mapped[float] = mapped_column(Float, default=0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    preview_storage_key: Mapped[str] = mapped_column(String, default="")
    generated_at: Mapped[str] = mapped_column(String, default=utcnow)
    shop_message: Mapped[str] = mapped_column(Text, default="")

    findings: Mapped[list["TriageFinding"]] = relationship(back_populates="report", order_by="TriageFinding.id")
    file: Mapped[File] = relationship()


class TriageFinding(Base):
    __tablename__ = "triage_finding"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    triage_report_id: Mapped[str] = mapped_column(ForeignKey("triage_report.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)   # blocker | warning | note
    title: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    measurement_json: Mapped[str] = mapped_column(Text, default="{}")
    suggested_fix: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open")   # open | resolved | overridden
    overridden_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    overridden_reason: Mapped[str] = mapped_column(Text, default="")
    overridden_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    report: Mapped[TriageReport] = relationship(back_populates="findings")


class IntakeAnswer(Base):
    """What the customer told us on the intake form, as data."""
    __tablename__ = "intake_answer"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    field: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[str] = mapped_column(String, default=utcnow)


# --- email ingest ---------------------------------------------------------------------------

class InboundEmail(Base):
    """One message received at art@{slug}: accepted into a proof, held in
    quarantine (unknown sender), or rejected (authentication failed)."""
    __tablename__ = "inbound_email"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    provider_message_id: Mapped[str] = mapped_column(String, default="")
    from_email: Mapped[str] = mapped_column(String, default="")
    from_name: Mapped[str] = mapped_column(String, default="")
    to_address: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    auth_result: Mapped[str] = mapped_column(String, default="")   # dkim=pass … as seen
    status: Mapped[str] = mapped_column(String, default="quarantined")  # accepted | quarantined | rejected | rate_limited
    reason: Mapped[str] = mapped_column(String, default="")
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")  # [{name, storage_key, bytes, mime}]
    proof_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    received_at: Mapped[str] = mapped_column(String, default=utcnow)
    decided_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)


# --- the chase engine ------------------------------------------------------------------

class ReminderSchedule(Base):
    """One step of a cadence. account default rows have proof_id NULL;
    a proof can carry its own overrides."""
    __tablename__ = "reminder_schedule"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    proof_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cadence: Mapped[str] = mapped_column(String)          # intake | response
    step_index: Mapped[int] = mapped_column(Integer)
    offset_hours: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String, default="email")   # email | sms | task
    condition: Mapped[str] = mapped_column(String, default="always")  # always | not_opened | opened_no_response
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ReminderSend(Base):
    __tablename__ = "reminder_send"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    reminder_schedule_id: Mapped[str] = mapped_column(String)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    proof_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    channel: Mapped[str] = mapped_column(String)
    to_address: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="queued")   # sent | suppressed | failed
    suppressed_reason: Mapped[str] = mapped_column(String, default="")
    scheduled_for: Mapped[str] = mapped_column(String)
    sent_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failed_reason: Mapped[str] = mapped_column(String, default="")


# --- approval and evidence ---------------------------------------------------------

TOKEN_PURPOSES = ("proof", "certificate", "reconfirm", "intake", "triage_report")


class AccessToken(Base):
    __tablename__ = "access_token"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    proof_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    approval_record_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contact.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    purpose: Mapped[str] = mapped_column(String)
    version_scope: Mapped[str] = mapped_column(String, default="current")
    issued_at: Mapped[str] = mapped_column(String, default=utcnow)
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    revoked_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_used_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ProofEvent(Base):
    """Append-only, hash-chained. See `events.append`."""
    __tablename__ = "proof_event"
    __table_args__ = (UniqueConstraint("proof_id", "sequence"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    proof_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[str] = mapped_column(String)
    local_offset: Mapped[str] = mapped_column(String, default="")
    time_source: Mapped[str] = mapped_column(String, default="server")
    actor_type: Mapped[str] = mapped_column(String)  # contact | user | system
    actor_id: Mapped[str] = mapped_column(String, default="")
    token_hash: Mapped[str] = mapped_column(String, default="")
    ip: Mapped[str] = mapped_column(String, default="")
    user_agent_raw: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    prev_event_hash: Mapped[str] = mapped_column(String, default="")
    event_hash: Mapped[str] = mapped_column(String)

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")


class ApprovalRecord(Base):
    __tablename__ = "approval_record"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_version_id: Mapped[str] = mapped_column(ForeignKey("proof_version.id"), index=True)
    colorway_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    approved_at: Mapped[str] = mapped_column(String, default=utcnow)
    method: Mapped[str] = mapped_column(String, default="self_service")  # self_service | on_behalf
    on_behalf_channel: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    on_behalf_evidence: Mapped[str] = mapped_column(Text, default="")
    recorded_by_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signer_name_typed: Mapped[str] = mapped_column(String, default="")
    signer_email: Mapped[str] = mapped_column(String, default="")
    signer_ip: Mapped[str] = mapped_column(String, default="")
    signer_user_agent: Mapped[str] = mapped_column(Text, default="")
    terms_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    consent_text_rendered: Mapped[str] = mapped_column(Text, default="")
    terms_body_rendered: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    conditions_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    artifact_hashes_json: Mapped[str] = mapped_column(Text, default="{}")
    event_chain_head: Mapped[str] = mapped_column(String, default="")
    certificate_sha256: Mapped[str] = mapped_column(String, default="", index=True)
    certificate_signature: Mapped[str] = mapped_column(String, default="")
    certificate_storage_key: Mapped[str] = mapped_column(String, default="")

    @property
    def conditions_snapshot(self) -> dict:
        return json.loads(self.conditions_snapshot_json or "{}")

    @property
    def artifact_hashes(self) -> dict:
        return json.loads(self.artifact_hashes_json or "{}")


class ChangeRequest(Base):
    __tablename__ = "change_request"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_version_id: Mapped[str] = mapped_column(ForeignKey("proof_version.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String, default="freetext")  # pin | chip | freetext | attachment
    chip_code: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    pin_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pin_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    status: Mapped[str] = mapped_column(String, default="open")
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    resolved_in_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Message(Base):
    __tablename__ = "message"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    proof_id: Mapped[str] = mapped_column(ForeignKey("proof.id"), index=True)
    proof_version_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String)  # inbound (customer) | outbound (shop)
    author_type: Mapped[str] = mapped_column(String)
    author_id: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    read_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


# --- engine and sessions ---------------------------------------------------------

_APPEND_ONLY_TRIGGERS = [
    "CREATE TRIGGER IF NOT EXISTS proof_event_no_update BEFORE UPDATE ON proof_event BEGIN SELECT RAISE(ABORT, 'proof_event is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS proof_event_no_delete BEFORE DELETE ON proof_event BEGIN SELECT RAISE(ABORT, 'proof_event is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS approval_record_no_delete BEFORE DELETE ON approval_record BEGIN SELECT RAISE(ABORT, 'approval_record is retained'); END;",
]

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
    if config.DATABASE_URL.startswith("sqlite"):
        with engine.begin() as connection:
            for statement in _APPEND_ONLY_TRIGGERS:
                connection.execute(text(statement))


def _add_missing_columns() -> None:
    """Columns added after a table first shipped: `create_all` leaves an
    existing table alone, so add them with guarded ALTERs (License Admin's
    own migration style). Only additive changes are ever made here."""
    from sqlalchemy import inspect
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                if default is not None:
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {1 if default else 0}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    else:
                        ddl += " DEFAULT '" + str(default).replace("'", "''") + "'"
                connection.execute(text(ddl))


def session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
