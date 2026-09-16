# Deploying PiperStitch Proofs

Same shape as License Admin (see Core's DEPLOY.md): one Railway service
built from `proofs/Dockerfile`, a volume mounted at `/data` (SQLite database
and artifacts), TLS from the platform.

## Environment

| Variable | Value |
|---|---|
| `SESSION_SECRET` | 32+ random characters |
| `SESSION_COOKIE_SECURE` | `true` |
| `PUBLIC_BASE_URL` | `https://proofs.piperstitch.com` (the customer links are built from this) |
| `LICENSE_ADMIN_URL` | License Admin's public URL |
| `WEB_API_KEY` | License Admin's `WEB_API_KEY` (identity + saved projects) |
| `CORE_SERVER_URL` | the app server's URL |
| `CORE_API_KEY` | the app server's `WEB_API_KEY` (its `X-API-Key` for `/api/v1/internal/*`) |
| `SMTP_*` or `POSTMARK_*` | as License Admin |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PROOFS` | the same Stripe account as License Admin; a recurring $25 price; webhook endpoint `/webhooks/stripe` for `checkout.session.completed` and `customer.subscription.*` |
| `WEBHOOK_TOKEN` | shared secret in Postmark webhook URLs |
| `CERTIFICATE_SIGNING_KEY` | optional; base64url Ed25519 private key (32 bytes) |

## Core-side prerequisites (additive, already in Core's repo)

- `POST /api/v1/internal/export/{format}` on the app server — added for
  Proofs; key-guarded like `internal/digitize`.

## Postmark webhooks

- Bounces: `https://<proofs>/webhooks/postmark/bounce?token=<WEBHOOK_TOKEN>`
- Inbound mail: `https://<proofs>/webhooks/postmark/inbound?token=<WEBHOOK_TOKEN>`,
  with an inbound domain whose MX points at Postmark. Addresses are
  `art@{slug}.piperstitch.com` (wildcard subdomain MX) or `art+{slug}@<inbound domain>`.
  Postmark adds the `Authentication-Results` header the DKIM/DMARC rule reads.

## Backups

The volume's snapshots, plus (recommended) a nightly copy of `/data` to an
S3-compatible bucket with object lock. Nothing in `/data/artifacts` is ever
overwritten by the application (`storage.put` refuses a changed key).
