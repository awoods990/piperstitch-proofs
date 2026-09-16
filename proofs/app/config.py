"""Configuration -- every knob is an environment variable, read once at
import. Mirrors License Admin's config.py so the two services deploy the
same way (Railway, a /data volume, a .env locally).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# --- where things live ----------------------------------------------------
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./proofs.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "") or f"sqlite:///{DATABASE_PATH}"
# Generated artifacts (renders, PDFs, machine files, certificates). A
# directory on the volume for now; an S3-compatible bucket with object
# lock is the production target (PRD v1.1 change 2).
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "./artifacts")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
if "<" in PUBLIC_BASE_URL or " " in PUBLIC_BASE_URL or not PUBLIC_BASE_URL.startswith("http"):
    # A placeholder pasted into Railway must never end up in a customer's link.
    PUBLIC_BASE_URL = ""

# --- sessions ---------------------------------------------------------------
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE")

# --- PiperStitch Core (License Admin for identity + projects, the Vapor
# app server for the stitch engine) -----------------------------------------
LICENSE_ADMIN_URL = os.environ.get("LICENSE_ADMIN_URL", "").rstrip("/")
WEB_API_KEY = os.environ.get("WEB_API_KEY", "")          # License Admin's shared web-API key
CORE_SERVER_URL = os.environ.get("CORE_SERVER_URL", "http://127.0.0.1:8089").rstrip("/")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")        # the Vapor server's X-API-Key (its WEB_API_KEY)

# --- email ------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USE_SSL = _bool("SMTP_USE_SSL")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "PiperStitch Proofs <proofs@piperstitch.com>")
EMAIL_OUTBOX_DIR = os.environ.get("EMAIL_OUTBOX_DIR", "")  # when set, emails are written here instead of sent (dev/tests)
POSTMARK_API_TOKEN = os.environ.get("POSTMARK_API_TOKEN", "")
POSTMARK_FROM = os.environ.get("POSTMARK_FROM", "")
POSTMARK_MESSAGE_STREAM = os.environ.get("POSTMARK_MESSAGE_STREAM", "outbound")

# Shared secret for provider webhooks (Postmark bounces, inbound mail): the URL carries ?token=...
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# --- SMS / MMS (Twilio) -----------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
# Either a phone number (+1...) or a Messaging Service SID (MG...). A messaging
# service is what A2P 10DLC registration attaches to; prefer it in production.
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
SMS_OUTBOX_DIR = os.environ.get("SMS_OUTBOX_DIR", "")   # dev/tests: write instead of send

# --- billing (the Proofs add-on subscription; same Stripe account as License Admin) ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_PROOFS = os.environ.get("STRIPE_PRICE_PROOFS", "")      # the $25/month price id
PROOFS_PRICE_CENTS = _int("PROOFS_PRICE_CENTS", 25_00)

# --- certificates -------------------------------------------------------------
# Optional Ed25519 private key (base64url, 32 bytes) used to sign every
# certificate in addition to the hash chain. Without it certificates are
# still verifiable by hash; with it they are also verifiable offline.
CERTIFICATE_SIGNING_KEY = os.environ.get("CERTIFICATE_SIGNING_KEY", "")

# --- product defaults ---------------------------------------------------------
FREE_PROOFS_GRANTED = _int("FREE_PROOFS_GRANTED", 3)
PROOF_TOKEN_DAYS = _int("PROOF_TOKEN_DAYS", 30)
DEFAULT_RESPONSE_WINDOW_DAYS = _int("DEFAULT_RESPONSE_WINDOW_DAYS", 7)
SIGNIN_CODE_TTL_MINUTES = _int("SIGNIN_CODE_TTL_MINUTES", 15)
# Owner sign-in goes through License Admin (the same code the app uses);
# set false to let any email sign in with a Proofs-issued code (dev/tests).
REQUIRE_LICENSE_ADMIN_SIGNIN = _bool("REQUIRE_LICENSE_ADMIN_SIGNIN", "true" if LICENSE_ADMIN_URL else "false")


def bad_ascii(name: str) -> str:
    """A header value or URL with a non-ASCII character in it (a '→'
    copied off a screen, a curly quote) fails deep inside an HTTP call
    with a UnicodeEncodeError; name the variable instead."""
    value = os.environ.get(name, "")
    bad = [ch for ch in value if ord(ch) > 127 or ch in "\r\n\t"]
    return f"{name} contains {bad[0]!r} -- re-paste it" if bad else ""


def require_for_serving() -> list[str]:
    missing = [bad for bad in (bad_ascii(n) for n in ("WEB_API_KEY", "CORE_API_KEY", "LICENSE_ADMIN_URL", "CORE_SERVER_URL", "PUBLIC_BASE_URL", "SMTP_HOST")) if bad]
    if not SESSION_SECRET:
        missing.append("SESSION_SECRET")
    if LICENSE_ADMIN_URL and not WEB_API_KEY:
        missing.append("WEB_API_KEY")
    if not (SMTP_HOST or POSTMARK_API_TOKEN or EMAIL_OUTBOX_DIR):
        missing.append("SMTP_HOST or POSTMARK_API_TOKEN")
    return missing
