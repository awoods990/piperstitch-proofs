# Build plan and status

Phases as in the PRD, with what is built.

## Phase 1 · The spine — **built, 11 acceptance tests pass** (16 Sep 2026)

| PRD acceptance criterion | Status |
|---|---|
| Compose a proof from a Core project in under 60 s and send it | ✅ upload a `.stitchpilot` or pick a saved project; compose renders, exports 5 formats, writes the PDF and hashes all of it |
| Customer opens on a phone, zooms, approves without an account | ✅ token link, mobile-first page, typed name + consent |
| Approval writes a certificate with SHA-256 of the PDF and every machine file, consent as displayed, IP, UA, verifiable chain | ✅ `approval_record` + Certificate PDF; chain head recorded |
| `GET /verify/{sha}` passes unauthenticated; one mutated byte of the stored PDF fails it | ✅ tested both ways |
| Second recipient can't reach the first's certificate; proof PDF on the link has no signer PII | ✅ certificate tokens are per-record; tested |
| Release produces a run ticket (stops, codes, hoop, backing, placement, quantity by size, stitch count) | ✅ |
| Sending v2 supersedes v1 and revokes its tokens in the same transaction; v1 readable via `/p/{token}/versions/1` | ✅ |
| Sales can't release; Stitcher can't send | ✅ |
| Disabling the entitlement leaves pages live, lets an in-flight approval complete, keeps certificates, blocks new sends | ✅ (free-proof exhaustion exercises the same gate) |
| Proof page scores WCAG 2.2 AA | ⏳ manual audit not yet done |

Also in: on-behalf approvals, questions/answers, decline, void, expiry
sweep + resend, release gate (hard/soft/off), settings with versioned terms,
seat invitations.

Garment context: 12 drawn templates with true-scale zones, mockup + measured
placement diagram on the page, the PDF and the run ticket.

**Deliberately not yet:** intake links,
triage, version compare view, colorways, SMS, reminders, sew-out log,
estimator, thread inventory, internal review, Stripe checkout for the add-on
(Phase 1 ships the three free proofs).

## Phase 2 · Intake and iteration — in progress (16 Sep 2026)

| PRD acceptance criterion | Status |
|---|---|
| Customer uploads a 12 MP phone photo of a business card; triage reports effective PPI at the requested size, detects the white background, counts colours, flags fine detail with a mm measurement | ✅ (`triage.analyze`; the "text under 5 mm" check is a measured narrowest-stroke check — glyph-level text detection is not yet done) |
| Forwarding a customer email creates a proof with attachments extracted | ⏳ email ingest not built |
| DKIM-failing / unknown senders rejected or quarantined | ⏳ with email ingest |
| A blocker prevents composition until resolved or overridden; the override appears on the proof and in the event chain | ✅ |
| Change requests land as a checklist with pins; Core overlays the pins | ✅ checklist with normalized pins (Core overlay is a later Core hook) |
| Version compare renders v1 against v2 with a difference view on a phone | ◐ side-by-side on the proof page; slider/difference views not yet |
| Both reminder cadences fire on schedule, respect quiet hours, suppress correctly, cancel on customer action | ✅ email + shop task steps (`reminders.py`); SMS steps are logged as suppressed until Phase 3 |
| Internal reviewer can fail a proof back with a note; can't review own version | ✅ |
| A hard bounce raises an in-app banner within 60 s | ✅ via the Postmark bounce webhook (`/webhooks/postmark/bounce?token=`) → board banner + shop email |

Also in: intake links with the default question set (answers land as data, SMS
consent evidence on the contact), shop direct upload, single-use intake
tokens with expiry sweep, the shareable read-only Readiness Report link.
AV scanning is recorded as `skipped` until a clamd sidecar is deployed.
## Phase 3 · The shop's daily tools
## Phase 4 · Polish and scale
