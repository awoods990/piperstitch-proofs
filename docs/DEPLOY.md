# Deploying PiperStitch Proofs (staging first)

Same shape as License Admin (see Core's DEPLOY.md): one Railway service built
from `proofs/Dockerfile`, a volume at `/data` (SQLite database and artifacts),
TLS from the platform. Nothing here touches Core's services except reading
two secrets they already have.

## 1. GitHub

Create an empty repository `awoods990/piperstitch-proofs` (private). The local
repo already has `origin` pointing at it; then:

```bash
cd ~/Desktop/PiperStitch\ Proofs && git push -u origin main
```

The `tests` workflow runs the 27 acceptance tests on every push.

## 2. Railway service

Railway → the existing PiperStitch project → **New service → GitHub repo →
`piperstitch-proofs`**. Then:

- **Settings → Source:** root directory `proofs` (Railway finds `railway.json`
  and the Dockerfile there).
- **Settings → Volumes:** mount path `/data`.
- **Settings → Networking:** generate a domain, or attach
  `proofs.piperstitch.com` (a CNAME at GoDaddy). Use it as `PUBLIC_BASE_URL`.

## 3. Variables

| Variable | Value |
|---|---|
| `SESSION_SECRET` | 32+ random characters (`openssl rand -base64 32`) |
| `SESSION_COOKIE_SECURE` | `true` |
| `PUBLIC_BASE_URL` | `https://proofs.piperstitch.com` (customer links are built from this) |
| `LICENSE_ADMIN_URL` | License Admin's public URL |
| `WEB_API_KEY` | **the same value** as License Admin's `WEB_API_KEY` (identity + saved projects) |
| `CORE_SERVER_URL` | the app server's public URL |
| `CORE_API_KEY` | **the same value** as the app server's `WEB_API_KEY` (its `X-API-Key` for `/api/v1/internal/*`) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | as License Admin (Microsoft 365), or |
| `POSTMARK_API_TOKEN`, `POSTMARK_FROM` | Postmark instead of SMTP |
| `WEBHOOK_TOKEN` | random string; goes in the Postmark webhook URLs below |
| `STRIPE_SECRET_KEY` | the same Stripe account as License Admin (test keys for staging) |
| `STRIPE_PRICE_PROOFS` | a recurring **$25/month** price created in that account ("PiperStitch Proofs") |
| `STRIPE_WEBHOOK_SECRET` | from the webhook endpoint in step 5 |
| `CERTIFICATE_SIGNING_KEY` | optional: base64url of 32 random bytes (Ed25519 seed) to sign certificates |
| `FREE_PROOFS_GRANTED` | `3` (default) |

Leave `REQUIRE_LICENSE_ADMIN_SIGNIN` unset: with `LICENSE_ADMIN_URL` set it
defaults to on, so owners sign in with their PiperStitch email code.

## 4. Core-side prerequisite (already in Core's repo, deploy it)

`POST /api/v1/internal/export/{format}` (Core commit `15ffe65`) and
`POST /api/v1/internal/build-from-artwork` (Core commit `b511eee`) on the app
server. Redeploy the app service so both routes are live. No other Core change
is needed.

## 5. Stripe

- Products → add **PiperStitch Proofs**, recurring, $25.00 / month → copy the
  price id into `STRIPE_PRICE_PROOFS`.
- Developers → Webhooks → add endpoint `https://<proofs>/webhooks/stripe` for
  `checkout.session.completed`, `customer.subscription.created`,
  `customer.subscription.updated`, `customer.subscription.deleted` → copy the
  signing secret into `STRIPE_WEBHOOK_SECRET`.

## 6. Postmark (optional but needed for bounces and art-by-email)

- Bounce webhook: `https://<proofs>/webhooks/postmark/bounce?token=<WEBHOOK_TOKEN>`
- Inbound: create an inbound stream; webhook
  `https://<proofs>/webhooks/postmark/inbound?token=<WEBHOOK_TOKEN>`. Either
  point an MX for `*.piperstitch.com` (wildcard) at Postmark so
  `art@{slug}.piperstitch.com` works, or use Postmark's inbound address with
  plus-addressing (`art+{slug}@…`) — both are recognised.

## 6b. Twilio (texting)

Variables: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` (a Messaging
Service SID `MG…` is preferred; a bare `+1…` number also works). Set the
number's or service's inbound webhook to
`https://<proofs>/webhooks/twilio/inbound` (POST); the signature is verified
with the auth token.

**Before any text will deliver to a US number:** Twilio → Messaging →
Regulatory Compliance → register an **A2P 10DLC brand** (your business) and
a **campaign** ("customer notifications: proof links and reminders, opt-in
collected on a web form, STOP to opt out"), then attach the number to the
messaging service. Carrier review takes days to a few weeks and cannot be
skipped — an unregistered number fails with error 30034. A verified
toll-free number is the alternative for low volume. Sample opt-in language
and the STOP flow are already in the product; use them on the form.

## 7. Smoke test on staging

1. Open `https://<proofs>/signin`, enter your PiperStitch email, use the code
   PiperStitch emails you → the board should open (this proves the License
   Admin path).
2. New proof → pick one of your saved projects → Compose → Send to yourself.
3. Open the link on your phone, approve, check the certificate email and
   `/verify/<sha>`.
4. Release → run ticket PDF.
5. Settings → Upgrade → Stripe test card `4242…` → the plan card should flip.

## Backups

The volume's snapshots, plus (recommended) a nightly copy of `/data` to an
S3-compatible bucket with object lock. Nothing in `/data/artifacts` is ever
overwritten by the application (`storage.put` refuses a changed key).
