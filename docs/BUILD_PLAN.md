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

**Deliberately not yet:** garment compositing (12 templates), intake links,
triage, version compare view, colorways, SMS, reminders, sew-out log,
estimator, thread inventory, internal review, Stripe checkout for the add-on
(Phase 1 ships the three free proofs).

## Phase 2 · Intake and iteration — next
## Phase 3 · The shop's daily tools
## Phase 4 · Polish and scale
