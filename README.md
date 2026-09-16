# PiperStitch Proofs

The paid proofing and approval add-on for PiperStitch: collect artwork,
build a stitch-accurate proof from the real digitized file, send it to the
customer on a page that needs no login, capture a legally defensible
approval with a verifiable certificate, and release a production packet.

The full specification is `docs/PRD.md` (v1.1 — the accepted-changes block
at the top wins over the v1.0 text). The build plan and status is
`docs/BUILD_PLAN.md`.

## Relationship to PiperStitch Core

Proofs is a separate service with its own database. It never modifies Core
and Core's repository is not on its build path. It talks to Core over HTTP:

- **License Admin** (`/api/web/*`, shared `WEB_API_KEY`) for who the shop is,
  what it is entitled to, and its saved projects.
- **The app server** (`/api/v1/internal/digitize`, `/api/v1/internal/export/{format}`,
  shared `X-API-Key`) for the stitch engine: the plan, the colour sequence,
  statistics and machine files. Locally, Core's server run without accounts
  exposes the same routes unguarded under `/api/v1/`.

The design hash, the thread stop list, the deterministic render, the PDF,
the certificate and the evidence chain are all Proofs' own.

## Run it locally

```
cd proofs
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env            # edit SESSION_SECRET
uvicorn app.main:app --port 8100
```

With `LICENSE_ADMIN_URL` blank, any email signs in with a Proofs-issued code
(written to `EMAIL_OUTBOX_DIR`) and becomes an owner. Point `CORE_SERVER_URL`
at a locally running Core server (`swift run` in Core's `server/`) to compose
real proofs; the tests don't need one.

## Tests

```
cd proofs && .venv/bin/python -m pytest -q
```

The suite is the PRD's Phase 1 acceptance criteria, end to end through the
HTTP surface, against captured fixtures (`tests/fixtures/` — a real cap-logo
project, Core's digitize response for it, and the five machine files).

## Deploy

Railway, root directory `proofs/`, a volume at `/data`, the environment in
`.env.example`. See `docs/DEPLOY.md`.
