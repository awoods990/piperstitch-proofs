# PiperStitch Proofs

PRODUCT REQUIREMENTS & BUILD SPECIFICATION

Version 1.1 · September 16, 2026 · Prepared for Ashley Woods · Status: **accepted for build** (v1.0 was "for review")

> **Revision 1.1 — accepted changes (Ashley Woods, 16 Sep 2026).** The v1.0 text
> below is kept verbatim; where it conflicts with this block, this block wins.
>
> 1. **Stitch handling uses PiperStitch Core's own Swift engine, not Python/pystitch.**
>    Core already reads and writes DST/PES/EXP/JEF/VP3, renders, and exposes its
>    engine over HTTP (the Vapor server's `/api/v1/digitize`, `/export/{format}`
>    and the key-guarded `/api/v1/internal/*` routes). Proofs calls those; it never
>    reimplements a reader, writer or digitizer. The proof render and the released
>    machine file therefore come from the same bytes by construction (principle 1).
>    Python remains the right tool for the Phase 2 *intake* pipeline only (AV scan,
>    HEIC/PSD conversion, EXIF/ICC, image analysis). "Python is non-negotiable" in
>    Technical architecture applies to intake, not to stitch handling.
> 2. **Database and hosting follow the existing License Admin pattern:** FastAPI +
>    SQLite on a Railway volume, Stripe, Microsoft 365 SMTP, Postmark optional —
>    with SQLAlchemy from day one so a move to Postgres is a connection-string
>    change. The Postgres-specific guarantees are met as follows: `proof_event`
>    append-only is enforced by `BEFORE UPDATE`/`BEFORE DELETE` triggers that abort,
>    plus the hash chain, plus the certificate carrying every hash (verifiable
>    independently of the database); approved artifacts go to S3-compatible object
>    storage with object lock; recovery is the volume's snapshots plus a nightly
>    copy of the database file to the same bucket.
> 3. **Rendering, Phase 1:** the customer-facing render is Core's deterministic
>    stitch render (visible stitch texture, hashed at creation). The instanced
>    textured-quad / THREE.js render in Technical architecture is a later upgrade to
>    the *look*; it must not change the hash contract or the artifact key set.
> 4. **PDF, Phase 1:** generated with a PDF library (selectable text for specs and
>    the thread list, the render embedded as an image), not headless Chromium. The
>    Chromium/Playwright route stays available if a shop needs pixel-perfect CSS.
> 5. **Proofs is a separate repository, service and database** (`PiperStitch
>    Proofs`, this repo). Core is consumed through its HTTP API; Core's repository is
>    not on Proofs' build path. Core gains only small additive hooks, each landed as
>    its own reviewed commit: an `/api/v1/internal/export/{format}` route for
>    server-to-server machine-file export (Phase 1), and later the colorways panel,
>    `design_hash` / `project.design_changed`, and the "Send for approval" button.
>    `design_hash` is computed in Proofs from the plan Core returns until Core emits it.
> 6. **Identity reuses License Admin's web API.** A shop signs in to Proofs with the
>    same email-code flow (`/api/web/signin/*`), and Proofs reads the account's
>    entitlement and saved projects through `/api/web/session` and
>    `/api/web/projects/*` with the shared `WEB_API_KEY`. `account_entitlements`
>    for the Proofs add-on lives in Proofs' own database keyed by License Admin's
>    customer id; the add-on's Stripe price is a Phase 2 item — Phase 1 ships the
>    three free proofs.
> 7. **Shop UI, Phase 1:** server-rendered pages (Jinja, as License Admin's
>    dashboard is), mobile-first. The React toolchain from Core's `web/` can replace
>    the pipeline board later if its interaction outgrows server rendering.
>
> 8. **Garment context, Phase 1:** twelve *drawn* garment templates (silhouette,
>    seams, shading, tinted to any garment colour) with zones carrying real-world
>    scale, rather than photographs with displacement maps. Honest by design
>    (principle 2 — a drawing is obviously a drawing) and colourable without a
>    photo per colour; photo templates use the same zone model later.
> 9. **Artifact key set** gains two optional keys, `mockup` and `diagram` (the
>    garment mockup and the placement diagram), alongside `{pdf, render, hero,
>    social, machine_files}`; `approval_record.artifact_hashes` uses the same set.
>
> Open questions 1–4 in the v1.0 text remain open.

## Executive summary

PiperStitch turns artwork into a machine-ready stitch file in seconds. It stops at the file. But the file is not where an embroidery job actually stalls — the job stalls in the eleven days between "can you do this logo?" and "yes, go ahead."

PiperStitch Proofs is a paid upgrade that owns that gap. It collects the customer's artwork, tells the embroiderer what is wrong with it before any work starts, builds a stitch-accurate proof from the real digitized file, sends it to the customer on a branded page that needs no login, captures a legally defensible approval, manages revision rounds without losing track of which version was approved, and releases a production packet to the embroiderer the moment the answer is yes.

Three findings from the market research make this a strong product rather than a nice feature:

| 1. No stitch-aware proofing product exists. Every proofing tool in decorated apparel — Printavo, GraphicsFlow, InkSoft, ProofStuff, shopVOX, YoPrint — treats the design as a flat image. None of them renders a DST with thread colors, stitch count, dimensions and density. PiperStitch already owns the stitch engine; nobody else in the proofing category can follow. 2. No approval record in apparel is legally defensible. The whole industry records "who clicked approve, and when" in a database row. Timestamp plus IP plus a SHA-256 of the exact approved artifact plus an exportable certificate exists only in e-signature products and in the $250+/month enterprise tiers of Ziflow, Filestage and PageProof. The shops with the most reprint disputes have the weakest evidence. 3. The $20–50/month tier is vacant. Apparel-native approval starts at $99 (GraphicsFlow) or $112 (ShopWorks ProofStuff). Generic per-seat proofing reaches $15–19 but knows nothing about garments, placements, thread charts or stitch counts. PiperStitch's customer — a one-to-five-person embroidery shop already paying $24 — will not buy a $99 add-on. They will buy a $25 one. |
|---|


The time being spent is real. Decorator research puts mockup production at 10–15 hours per week for a small shop, against an average of seven revisions per order and an 11-day sales cycle from inquiry to close. Proofing is not a side task; it is a day and a half of every working week.

### What this document contains

This is a complete build specification: module boundaries, personas, the end-to-end workflow, the proof lifecycle state machine, the full data model, the API surface, the rendering architecture, a screen-by-screen UI spec, the notification matrix, security and compliance requirements, packaging and pricing, and a four-phase build plan with acceptance criteria. It is written to be handed to Claude Code and built.

## Product principles

Seven decisions that resolve most of the smaller ones later.

1. The proof is generated from the stitch file, never drawn beside it. A proof image that is not a render of the actual machine file is a liability. Every PiperStitch proof is rendered from the exact .dst/.pes bytes that will be released to production, and the render and the file share a hash. This is the product's core claim and it must never be compromised for convenience.

2. Honest rendering beats beautiful rendering. Industry sources are explicit that "embroidery inherently distorts during stitching, making screen-based designs misleading," and that a too-perfect preview manufactures disputes. PiperStitch renders with visible stitch texture, shows the real thread count and density, and states plainly what the proof does not promise. A proof that oversells is a reprint waiting to happen.

3. The customer never creates an account. A magic link to a mobile-first page. No password, no app, no "create an account to approve your logo." Every point of friction between the link and the tap is measured in days of delay.

4. Approval is bound to conditions, not to a design. An approval covers one design version, on one garment and color, at one size, in one placement, on one fabric. Change any of those and the approval no longer applies — the system says so and offers a one-tap re-approval rather than silently reusing the old consent. This is the single most valuable rule in the product and no competitor implements it.

5. Every state change is an evidence record. Sent, delivered, opened, viewed, scrolled, commented, approved — each with a timestamp, an IP, a user agent, the consent text as displayed, and a hash chain linking it to the previous event. The Certificate of Approval is a first-class deliverable, not a debug log.

6. The embroiderer's default action is one tap. The shop owner is running a machine, not a desk. Every recurring action — send, remind, approve on the customer's behalf after a phone call, release to production — is one tap from the pipeline board on a phone.

7. Proofs degrades gracefully. If the subscription lapses, nothing is destroyed and nothing is hidden from the shop. Links a customer already holds keep working, an approval already in flight can still be completed, and certificates stay downloadable forever. Only sending new proofs is gated.

## Module architecture and packaging

PiperStitch becomes three modules on one account. This section defines what belongs where, so the Proofs upgrade can ship without waiting on the customer-management upgrade, and so the two can be built by different people at different times.

### Module boundaries

| Module | Owns | Status |
|---|---|---|
| PiperStitch Core | Artwork import, auto-digitizing, object editing, stitch simulation, fabric and hoop settings, thread palette, machine-file export, Projects | Shipping today, $24/mo |
| PiperStitch Proofs | Art intake, art triage, proof composition, customer proof page, revision cycles, approval capture, certificates, chase engine, production release, sew-out log, estimator, placement library | This document |
| PiperStitch Clients | Customer records, contacts, saved art library, order and job records, quotes, invoices, deposits, payment links, reorder history, per-customer pricing, reporting | Separate specification, built later |


The boundary rule, stated precisely. Proofs owns the quantity, sizes, placement, garment and estimated price of the thing being approved, because those facts are part of what the customer consented to and must be immutable once approved. Clients owns orders, quotes, invoices, payment and customer history. A request for an invoice, a deposit, a payment link or a customer ledger is a Clients request. A request to show what was approved is a Proofs request, even when it involves a number with a dollar sign.

### The seam between Proofs and Clients

Proofs needs somebody to send a proof to. It does not need a CRM. The resolution is a deliberately thin shared object.

| contact — the minimal party record, owned by Core and available to every module. Fields: id, account_id, display_name, company_name, email, phone, preferred_channel, timezone, sms_opt_in_at, sms_opt_in_text, opted_out_at, created_at, notes (single free-text field), archived_at. That is the whole thing: identity, how to reach them, and the delivery-consent fields that quiet hours and carrier compliance legally require. No addresses, no tax IDs, no terms, no order history, no pricing tier, no tags. When Clients is activated, it creates a customer record that references contact one-to-one and extends it. No data migrates, no IDs change, nothing in Proofs is rewritten. contact.notes becomes the seed of the customer's first note. Proofs continues to read contact and never reads customer. |
|---|


The same rule governs the job. Proofs stores the garment and placement facts on the proof version itself, denormalized, because they are part of what was approved. Clients stores the same facts on an order_line for pricing and scheduling. When Clients is on, creating a proof from an order copies the values onto the proof; the proof's copy is still the legal record. They are allowed to diverge, and the proof always wins.

### What lives in Core even though Proofs uses it

Four capabilities move into Core as part of this work, because they make the free-standing product better and because Proofs depends on them:
- Named colorways on a project. Core already has a thread palette. It gains the ability to save multiple named colorways per design and switch between them.
- Server-side render service. Core's in-browser simulation gets a headless twin that produces deterministic PNG/SVG at specified dimensions. Proofs, email thumbnails, and PDF generation all consume it.
- Design fingerprinting. A stable design_hash over the stitch data, so "is this the same design the customer approved?" is answerable in constant time. Its pre-image is defined precisely in Integration.
- A project.design_changed internal event. Emitted whenever a project's design_hash changes, carrying {core_project_id, old_design_hash, new_design_hash, changed_at}. This is what conditions-changed detection subscribes to.

These ship to all Core subscribers. They are not gated.

### Entitlement model

A single account_entitlements record with boolean flags per module and a plan_code. The application checks entitlement at three layers — route guard, API middleware, and UI render — and never at only one. Behavior when proofs is off:
- Proofs navigation is visible but marked as an upgrade, with a live sample proof the shop can click through. Hiding a paid feature sells nothing.
- Existing proof records remain readable and exportable. Certificates remain downloadable.
- Public proof pages remain live and remain viewable by customers. An approval already in flight can still be completed — a customer who opened a proof before the lapse can approve it — but no new proof can be sent. Killing a link a customer already has, or stranding them mid-approval, is unacceptable.
- Transactional link-recovery email (the "request a new link" action on an expired page) remains exempt from the reminder freeze. Scheduled reminders do not fire.
- No new proofs can be created.

Downgrade never deletes. Data retention on cancellation follows the account-level policy in Security and compliance.

## Users and jobs

### Primary persona — Dana, owner-operator

One or two multi-needle machines in a converted garage or a 600-square-foot shop unit. Between four and twenty-five active jobs. Does the selling, the art, the digitizing, the hooping, the sewing and the invoicing. Works from a phone as much as a laptop, often standing at a machine.

What she is actually trying to do:
- Stop re-typing the same questions to every new customer.
- Find out that a logo has 3mm text before she spends forty minutes digitizing it.
- Send something that looks professional enough that the customer takes her seriously and pays her rate.
- Stop losing three days to a customer who never replied.
- Have something to point at when a customer says "that's not the color I picked."
- Know, at a glance, which of her twenty jobs are waiting on her and which are waiting on someone else.

### Secondary persona — Marcus, the customer

Buys embroidery two to six times a year. A restaurant owner, a youth league coordinator, an HVAC company office manager. Not a designer. Sends a logo he found in an email signature. Reads the proof on his phone, in a parking lot, between other things.

What he needs:
- To understand what he is looking at in under ten seconds.
- To not create an account.
- To be able to say "the blue is wrong" without knowing what a thread number is.
- To feel that approving is safe and reversible until the machine starts.

### Tertiary persona — Ray, the part-time stitcher

Works Tuesdays and Thursdays. Runs jobs Dana has already sold. Needs a run ticket that tells him everything without asking Dana a question: file, thread stops in order, hoop, backing, placement measurements, garment count by size, and whether it is cleared to sew.

### Jobs-to-be-done statements

| When… | I want to… | So I can… |
|---|---|---|
| A new inquiry arrives with a phone-photo logo | send one link that collects the art and the job details | stop the eleven-message back-and-forth |
| I receive artwork | know immediately whether it can be stitched at the requested size | avoid quoting a job that will fail |
| I finish digitizing | show the customer what it will actually look like sewn | set expectations that the sew-out will meet |
| The customer goes quiet | have the system chase them for me | stop spending my evenings on follow-up texts |
| The customer approves | have proof that they approved this exact version | not eat the cost of a reprint |
| Approval lands | hand my stitcher a complete run ticket | start sewing without another conversation |
| The same customer reorders | reuse the approval when nothing changed, and re-confirm when it did | move fast without taking on risk |


## The end-to-end workflow

The narrative spine of the product. Every feature in this document exists to serve one of these nine steps.

| 1 · Request — Dana creates a proof and sends an intake link, or forwards the customer's email into PiperStitch. 2 · Intake — Customer uploads artwork and answers the job questions on a branded form. Files land in a quarantined bucket, get scanned, converted, and analyzed. 3 · Triage — PiperStitch produces an Art Readiness Report: effective resolution at requested size, vector or raster, transparency, color count, and specific embroidery risks with measurements. 4 · Digitize — Dana opens the art in PiperStitch Core. Everything she already does. Saves colorways. 5 · Compose — The proof builds itself from the project: stitch render, garment mockup, thread stop list, dimensions, stitch count, placement diagram, terms. Dana adjusts and adds a note. 6 · Send — Email and optional SMS with a magic link. Delivery, open and view are all tracked. 7 · Respond — Customer approves, requests changes with markup, or asks a question. Or goes silent, and the chase engine works on Dana's behalf. 8 · Iterate — Change requests become a checklist. Dana revises in Core, creates version 2, and sends it with a side-by-side compare against version 1. 9 · Release — Approval writes a certificate, unlocks the production packet, notifies Dana and Ray, and moves the job onto the sewing board. |
|---|


## The proof artifact

What is actually on a PiperStitch proof. This is the product's differentiation made concrete, so it deserves its own specification.

### Required elements

Every proof, at every version, contains all of the following. None are optional, because every one of them is a dispute that shops currently absorb.

| Element | Content | Why it is there |
|---|---|---|
| Stitch render | Rendered from the actual machine file at true scale, with visible stitch texture and direction | The core claim: this is not a picture of your logo, it is a picture of your logo sewn |
| Garment context | The render composited onto the actual garment style and color, warped to the fabric, at correct relative size | Customers cannot judge a floating logo. Size shock is the most common late-stage objection |
| Finished dimensions | Width × height in inches and millimeters, to two decimals | Placement and size disputes |
| Stitch count | Total stitches, plus color-change and trim counts | Drives price and run time; customers who see it argue about price less |
| Thread stop list | Ordered sequence of stops — stop number, brand, thread code, thread name, swatch, and stitches in that stop | A DST carries no thread names. This list is the missing half of the file |
| Placement diagram | Measured callouts — e.g. "4.0 in below shoulder seam, 3.0 in from center front" — on a garment outline | Printavo's model terms allow 0.5 in placement variance; showing the target makes the tolerance fair |
| Quantity and sizes | Item count with the size breakdown | Part of what is being approved; drives the run ticket |
| Fabric and backing | Fabric selected in Core, plus recommended stabilizer | A proof approved on a twill cap is not valid on a fleece pullover |
| Version stamp | "Version 2 of 2 · created Sept 14, 2026 · supersedes v1" | Version confusion is a named top-three complaint |
| Terms block | The shop's approval terms, versioned, shown in full above the approve button | Consent has to be visible at the moment of consent to be worth anything |
| Honesty note | Fixed, non-removable statement of what a screen proof cannot guarantee | Principle 2. See Appendix A for the default text |


### Optional elements, toggled per proof
- Alternate colorways — up to four named colorways side by side, with the customer selecting one as part of approval. Approval records the chosen colorway.
- Sew-out photograph — a real photo of a test sew, uploaded from the shop's phone. When present it is displayed above the render, because a photograph outranks a simulation and the industry's strongest existing proof artifact is a scanned sew-out.
- Density map — a heat overlay showing stitch density, for customers who ask technical questions. Off by default.
- Stitch-order animation — a short playback of the sew sequence. Delightful, occasionally useful for explaining why a design costs what it costs.
- Price line — the estimator's figure for this run, from the shop's own per-thousand rate. When Clients is active this is enriched with the quoted price, terms and deposit status from the order; without Clients it is the estimate alone.

### Formats produced

Each proof version generates, atomically, at creation time:
- Web proof page — responsive HTML, the primary artifact.
- Proof PDF — print-ready, one or two pages, with every element above as selectable text (not raster) for the specs and thread list.
- Email hero image — 1200 × 630 PNG for the notification email and link previews.
- Square social image — 1080 × 1080 PNG, because shops post proofs.
- Machine file bundle — the stitch file in the requested formats plus a .col sidecar. Availability follows the account's release_gate_policy: hard withholds the bundle entirely until release; soft allows download with an unapproved-status banner and records an override event; off places no restriction. Default for new accounts is soft.

All five are hashed with SHA-256 at creation and written to proof_version.artifact_hashes under the fixed key set {pdf, render, hero, social, machine_files: {dst, pes, exp, jef, vp3}}. approval_record.artifact_hashes uses the identical key set so the two are directly comparable. If any byte changes, the certificate is invalid and the system says so.

## Proof lifecycle state machine

The proof is the state machine. Everything else in the system reads from it.

### Two scopes, deliberately

State is tracked at two levels, because a proof and the versions inside it move independently. Version 1 can be superseded at the same instant version 2 becomes sent, and one status column cannot hold two values.
- proof_version.status is authoritative for everything the customer touches.
- proof.status is a denormalized rollup used by the pipeline board and list filters. It is derived, never hand-set, and always equals either a proof-scoped state or the status of current_version_id.

### Proof-scoped states

| State | Meaning | Who is waiting | Terminal |
|---|---|---|---|
| draft | Created, not yet sendable | Shop | No |
| awaiting_art | Intake link sent, no usable artwork received | Customer | No |
| intake_expired | Intake window elapsed with no artwork | Shop | No, reopenable |
| art_received | Files received, triage complete or in progress | Shop | No |
| digitizing | Linked to a Core project, being worked | Shop | No |
| internal_review | Optional second-set-of-eyes stage before the customer sees it | Shop | No |
| released | Production packet unlocked and handed off | Shop | No, voidable |
| completed | Job sewn and closed | Nobody | Yes |
| void | Cancelled by the shop | Nobody | Yes |


### Version-scoped states

| State | Meaning | Who is waiting | Terminal |
|---|---|---|---|
| ready_to_send | Composed, not yet delivered | Shop | No |
| sent | Delivered to customer, not yet opened | Customer | No |
| viewed | Proof page opened at least once | Customer | No |
| changes_requested | Customer asked for revisions | Shop | No |
| approved | Customer approved, conditions unchanged | Shop | No |
| approved_with_notes | Approved, with non-blocking comments attached | Shop | No |
| declined | Customer cancelled the job at proof stage | Nobody | No, reopenable |
| expired | Response window elapsed with no answer | Shop | No, reopenable |
| superseded | A newer version of this proof was sent | Nobody | Yes |


### Transitions

| From | Event | To | Guard |
|---|---|---|---|
| draft | send_intake | awaiting_art | Contact has email or phone |
| draft | attach_art | art_received | At least one file |
| awaiting_art | art_uploaded | art_received | Passes AV scan |
| awaiting_art | expire_intake | intake_expired | intake_window_days elapsed |
| intake_expired | resend_intake | awaiting_art | New intake token minted |
| art_received | link_project | digitizing | Core project exists and belongs to the account |
| digitizing | compose | ready_to_send (v1) | Design hash present, blocking triage findings resolved or overridden |
| ready_to_send | request_internal_review | internal_review | Account has more than one seat |
| internal_review | pass_internal | ready_to_send | Reviewer is not proof_version.composed_by |
| internal_review | fail_internal | digitizing | Reviewer note captured |
| ready_to_send | send | sent | Terms version set, delivery channel valid, entitlement active |
| sent | open | viewed | Token valid and not expired |
| sent | bounce | sent | Records failure, raises alert, does not change state |
| viewed | approve | approved | Token valid, consent accepted, conditions unchanged |
| viewed | approve with notes | approved_with_notes | As above, plus note text present |
| viewed | request_changes | changes_requested | At least one comment or annotation |
| viewed | decline | declined | Reason captured |
| sent, viewed | expire | expired | response_expires_at passed |
| expired, declined | resend | sent | New token minted, old token revoked |
| changes_requested | new_version | ready_to_send (v+1) | — |
| sent, viewed, changes_requested, expired, declined | supersede | superseded | Side effect only. Fires on every version below N when version N enters sent, in the same transaction. Revokes the superseded version's tokens. Never invoked directly. |
| approved, approved_with_notes | release | released (proof) | Certificate written, artifact hashes verify |
| approved, approved_with_notes | conditions_changed | ready_to_send (v+1) | Garment, color, size, placement, fabric or design hash changed; prior approval flagged as not covering the change |
| released | complete | completed | Shop marks sewn |
| Any non-terminal state | void | void (proof) | Shop action, reason captured, customer notified, live tokens revoked |


### Invariants

| A proof version, once sent, is immutable. Corrections create a new version. There is no "edit and resend the same version." Exactly one version of a proof may be in a live customer-facing state at a time. Sending v2 fires supersede on v1 and revokes v1's action tokens within the same transaction. A response token is bound to one proof version and one recipient. An intake token is bound to a proof with no version. A certificate token is bound to one approval record and never expires while the record is retained. released cannot be reached without a row in approval_record whose proof_version_id matches the version being released and whose artifact_hashes still verify. approved is a property of a version, never of a proof. "Is this proof approved?" is always answered as "version N is approved, and the current version is N." Terminal states (completed, void, superseded) accept no further transitions. declined and expired are dormant, not terminal — both reopen via resend. |
|---|


## Feature specification

### 5.1 Art intake

The three intake paths, in order of expected volume.

Intake link. Dana taps New Proof, picks or types a contact, and sends a link. The customer lands on a branded, mobile-first form:
- Drag-drop or camera upload, multiple files, up to 400 MB each, resumable
- Accepted: PNG, JPG, HEIC, SVG, PDF, AI, EPS, PSD, TIFF, WEBP, plus DST/PES/EXP/JEF/VP3 if they have a file already
- Job questions from the account's intake_question_set, each mapped to a proof field so answers land as data rather than prose, and each with a sensible default so the form stays short: what item is this going on, what color, how many (with a size grid), how big should the logo be, where on the item, when do you need it
- Free-text "anything else we should know"
- Phone number collected with explicit SMS consent language, stored on contact.sms_opt_in_at and sms_opt_in_text. This is the opt-in evidence carriers audit.

Email ingest. Every account gets art@{shop-slug}.piperstitch.com. Dana forwards the customer's email; PiperStitch parses sender, subject, body and attachments into a new proof, with the email body preserved as the first note. Inline images are extracted; signature images and tracking pixels are filtered by size and repetition heuristics. This is the path shops will actually use most, because artwork arrives by email today and asking them to change that is asking too much.

| The address is guessable, so the route must be authenticated by the mail itself. Rules, all mandatory: Reject any message failing DKIM, or with dmarc=fail. No exceptions, no "best effort" mode. Trusted senders — addresses belonging to the account's own user rows, and any address matching an existing contact.email on that account — create a proof directly in art_received. Unknown senders land in a quarantine queue surfaced in-app as "3 emails waiting to be accepted," showing sender, subject and attachment thumbnails. Accepting creates the proof and optionally the contact. Nothing is auto-created from an unknown sender. Shop slugs are unique and immutable once issued; near-miss slugs are refused at creation. Rate limit: 50 messages per account per hour, 10 per sending address per hour, with overflow queued and the shop alerted. Attachments follow the same quarantine-scan-promote pipeline as uploads. |
|---|


Direct upload. Dana uploads from her own device. The common case for repeat work and for art she has cleaned up herself.

MMS ingest is a phase-three addition, gated on the A2P 10DLC work described in Notifications.

### 5.2 Art triage — the Readiness Report

Runs automatically on every uploaded file. This is the feature that saves the most wasted work and the one no competitor has.

File-level analysis

| Check | Method | Output |
|---|---|---|
| Effective resolution | Pixel dimensions ÷ requested finished size | "Your logo is 1,080 px wide. At 3.5 in that is 309 PPI — good." or "…is 88 PPI — too low to trace cleanly." |
| Vector or raster | PDF/AI content-stream inspection: presence of curves, lines, rects, text vs a single full-page image | "This PDF is a scan, not vector art" |
| Transparency | Alpha channel present and actually non-opaque; border-pixel sampling | "White box behind the logo — we will remove it" |
| Color count | k-means quantization in Lab space | "We count 6 distinct colors. Thread stops drive cost and run time." |
| Colorspace | ICC profile detection, Display P3 → sRGB conversion | Silent correction; logged |
| Orientation | EXIF | Silent correction |
| Format conversion | HEIC → PNG, PSD flatten, PDF page selection | Silent |
| Fonts | Missing-font detection in PDF/AI | "Two fonts aren't embedded — text may shift" |


Embroidery-specific risk checks. Each returns severity (blocker, warning, note), a plain-English explanation, a measurement, and a suggested fix. These thresholds come from published digitizing practice and should be configurable per account.

| Risk | Threshold | Message |
|---|---|---|
| Text too small | Cap height under 5 mm (≈3/16 in); lowercase x-height under 4 mm (≈5/32 in) | "The tagline is 3.1 mm tall. Below about 5 mm, letters close up and read as blobs. Options: raise the logo to 4.2 in wide, drop the tagline, or accept the risk." |
| Counter closure | Enclosed aperture under 0.80–1.00 mm at 40wt | "The centers of the e and the a will fill in at this size." |
| Satin too narrow | Column under 1.5 mm | "The outline drops to 1.1 mm at the top of the swoosh — thread will break there." |
| Satin too wide | Column over 12.1 mm | "This band is 14 mm wide; it will be converted to a fill, which looks different." |
| Detail separation | Under 0.50 mm between courses; under 0.80 mm for two lines to read separately | "These two rules will merge into one." |
| Color count vs needles | Stops greater than machine needle count | "9 colors on a 6-needle machine means a mid-run rethread." |
| Cap constraints | Height over 2.25 in; visor over 1.5 in; any element crossing the center seam | "On a structured cap this design exceeds the 2.25 in sew field, and the lowercase i falls in the seam valley." |
| Fabric fitness | Small lettering on pique, sheer, textured or silky fabric; ribbed knits without knockdown | "Small text on pique knit sinks into the texture. Consider a knockdown underlay." |
| Gradients | Detected smooth gradients or photographic regions | "Thread cannot fade. This gradient will become 3 flat colors." |
| Stitch count vs size | Estimated count far outside the placement norm | "18,400 stitches for a left chest is unusually heavy — the garment may pucker." |


The report is a shareable artifact in its own right. Dana can send it to the customer as "here's what we found in your file," which converts an awkward conversation into a professional deliverable and frequently converts a bad file into a paid art-cleanup job. Sending it mints a read-only purpose=triage_report token; the customer sees the findings and an optional shop message, and nothing else.

Blockers gate composition. A proof cannot be composed with unresolved blocker findings unless Dana explicitly overrides, and the override is recorded on the proof and surfaced to the customer as an advisory line. This protects the shop from itself.

### 5.3 Proof composition

Opens with the proof already assembled. Dana's job is to adjust, not to build.

Auto-assembled from the Core project: render at true scale, stitch count, color-change and trim counts, dimensions, thread stops in sew order, fabric, hoop.

Garment mockup. A catalog of garment templates, each a photograph plus a displacement map, a shading map and one or more placement zones. Ships with roughly forty templates covering the shapes that matter: crew tee, polo, oxford, hoodie, quarter-zip, fleece jacket, softshell, work shirt, apron, structured cap, unstructured cap, trucker, beanie, visor, tote, duffel, towel, blanket. Each in a range of colors. The design is warped into the zone with a displacement map and composited with a multiply shading pass plus a slight ambient-occlusion drop, because embroidery sits proud of the cloth by roughly half a millimetre.

Dana can also upload her own garment photo and set a placement zone by dragging four corners — essential for shops with a house catalog or an unusual item.

Placement. A placement picker seeded with industry defaults, each editable and savable to the shop's own placement library.

| Placement | Default size | Default position |
|---|---|---|
| Left chest | 3.5–4.0 in wide | 4 in below shoulder seam, 3 in from center front |
| Right chest | 3.5–4.0 in wide | Mirrored |
| Full front | 10–11 in wide | 4 in below collar |
| Jacket back | 10–12 in wide | 2 in below collar seam |
| Sleeve | 3.0 × 2.0 in | 4 in below shoulder seam |
| Cap front | 4.5 × 2.25 in | Centered, 0.5 in above brim |
| Cap side | 2.5 × 1.5 in | 1 in behind seam |
| Cap back | 4.0 × 1.0 in | Arched above closure |
| Beanie cuff | 4.0 × 2.0 in | Centered on cuff |
| Towel | 6.0 in wide | 2 in above hem |


The placement panel writes measured callouts onto the proof automatically. Changing the placement regenerates the diagram.

Colorways. Up to four named colorways, each a full thread-stop assignment. Dana can generate a colorway automatically for a given garment color — the system checks contrast in Lab space and warns when a thread is within a ΔE00 of about 8 of the garment, which reads as invisible on fabric.

Message. A per-proof note with saved templates, merge fields ({{customer_name}}, {{item}}, {{due_date}}), and an account default.

Terms. The shop's approval terms, selected by version. Editable in settings, with a sensible default supplied (Appendix A). Changing terms creates a new terms version; existing proofs keep the version they were sent with, permanently.

Response window. Default 7 days, configurable. Sets proof_version.response_expires_at and drives the chase cadence. This is the clock the state machine watches; the access token's own expiry is longer and separate, so that an expired proof still opens and still offers a fresh link.

### 5.4 The customer proof page

Mobile-first. Assume a phone, one hand, poor light, thirty seconds of attention.

Above the fold: shop logo and name; "Proof for review — Version 2"; the garment mockup, full-bleed; the item, color and quantity in one line; and a single sentence of instruction.

Scroll:
- Full-size stitch render — pinch-zoomable to 8×, so the customer can inspect the text they are about to approve. Toggle between "on the garment" and "the design".
- Colorway chooser when more than one is offered, as large tappable cards.
- Specs — dimensions, stitch count, placement diagram with measurements, fabric, backing.
- Thread stops — a list: swatch, stop number, brand and code, thread name, stitches. Tapping a swatch highlights that color in the render.
- The honesty note — plainly worded, always present.
- Terms — in full, not behind a link.
- Actions.

The three actions:

| Looks good — approve → a confirm sheet stating exactly what is being approved: design version, colorway, garment, color, size, placement, fabric. A required checkbox with the shop's consent language. Typed full name. Then Approve. On success, an immediate confirmation screen with a download link to the approval certificate and a copy emailed to them. Request changes → an annotation surface. The customer can tap a point on the render to drop a numbered pin and type a comment, or use quick-tap chips for the common cases: "color is wrong", "too big", "too small", "wrong spot", "text is wrong", "use a different logo". Quick-tap chips matter more than free text — most customers will not write a paragraph, and structured feedback is far easier for Dana to act on. Free-text and file attachment are both available. Ask a question → a message that does not change the proof state, delivered to Dana immediately, with a threaded reply on the same page. This exists because questions currently arrive by text message and never make it into the record. |
|---|


Also on the page: the version history, with earlier versions viewable read-only; a "request a physical sew-out" button when the shop has enabled it, showing the fee; and a print/download button for the proof PDF.

Accessibility and robustness. WCAG 2.2 AA. Never rely on color alone to convey a thread assignment — swatches always carry codes. The page must render usefully with images blocked and must work on a four-year-old Android phone on a poor connection: the hero image is served in progressive sizes, everything below the fold lazy-loads, and total above-the-fold weight stays under 300 KB.

### 5.5 Revision cycles

Seven revisions per order is the industry average. The product's job is to make revision seven cost the same as revision one.
- Change requests arrive as a checklist on the proof, each item with its pin location, the customer's words, and a status (open, addressed, declined, discussed).
- Dana works through the list in Core, then creates v2. The checklist travels to v2 with each item's resolution, and the customer sees "here's what changed" as an itemized list above the new render. This single element does more for approval speed than anything else on the page.
- Version compare — three modes: side-by-side with synchronized zoom, a swipe-slider overlay for mobile, and a difference view that highlights changed regions. Four apparel suites have zero compare modes; Approval Studio sells four for $60/month. Three done well is enough to lead this category.
- Revision counter and policy. The shop sets how many revisions are included; the proof shows the count; when the limit is passed, the customer sees the shop's stated additional-revision fee before submitting, and Dana gets a prompt to add the charge. Printavo's recommended model — first revision free, subsequent at an hourly rate — is the default.
- Round-trip time is measured on every cycle and surfaced in reporting, split into time-with-shop and time-with-customer. Shops chronically blame themselves for delays that are not theirs.

### 5.6 Approval capture and the certificate

The compliance heart of the product. Requirements derive from the ESIGN Act and UETA, adopted in 47 states plus DC.

The four legal requirements and how each is met:

| Requirement | Implementation |
|---|---|
| Intent to sign | A deliberate, labeled action — a confirm sheet, a required checkbox, a typed full name, then a button reading "Approve this proof." Not a single tap on a link. |
| Consent to electronic records | The consent text is displayed in full at the moment of approval, its terms_version_id recorded, and its exact rendered string stored on the approval record. Includes the right to request a paper copy and how. |
| Association of signature with record | SHA-256 of the proof PDF, the render, and each machine file, stored on the approval record under the same fixed key set as proof_version.artifact_hashes. A content-addressed verification endpoint re-hashes on demand. |
| Retention and reproduction | Versioned, object-locked storage; certificate retained seven years minimum and reachable by both parties for that whole period through a non-expiring certificate token. |


Every event recorded, from send to release: event_type, UTC timestamp with local offset and NTP source, actor, SHA-256 of the access token used (never the token), IPv4/v6 with reverse DNS, raw and parsed user agent, coarse IP geolocation with the GeoIP database version stamped, referring channel, page dwell time, monotonic sequence number, and prev_event_hash — a hash chain making retroactive insertion detectable.

The Certificate of Approval — a self-contained PDF generated at approval and stored as a file with kind=certificate:
- Cover: shop identity, customer identity, proof reference, version, approval timestamp.
- Exactly what was approved: the render thumbnail, design version, colorway, garment and color, finished size, placement, fabric, quantity and sizes, thread stop list.
- The full consent text as displayed, with its version.
- The complete event timeline with timestamps, IPs and user agents.
- Artifact hashes with the algorithm named, and the content-addressed verification URL /verify/{certificate_sha256} — a link the customer can actually open, because a verification endpoint only the shop can call protects nobody.
- The shop's terms as they stood at send.

Who can read it. The certificate contains the signer's name, email, IP, user agent and coarse location. It is served only through the non-expiring certificate token minted for that signer, and through the authenticated shop API. It is not appended to the proof PDF and is never reachable from a proof token, because a multi-recipient send would otherwise hand recipient B recipient A's IP address. Co-recipients who need the record receive a redacted copy from the shop with IP, user agent and geolocation removed.

On-behalf-of approval. Customers approve by phone, in person, and by text message constantly, and the system must not pretend otherwise. Dana can record an approval on the customer's behalf, and the certificate says so explicitly, names the method (phone, in person, text, email), requires her to attach evidence or a note, and stamps her identity as the recorder. A clearly-labeled second-hand approval is far more defensible than a fabricated first-hand one, and far more honest than the alternative shops currently use, which is nothing.

Conditions-changed detection. Principle 4, enforced. The approval record stores the full conditions_snapshot. Any later change to garment, color, size, placement, fabric, quantity or design hash raises a banner on the proof — "This approval doesn't cover the change you just made" — and offers a one-tap re-confirmation: a short page showing only what changed, with an approve button. Re-confirmation produces its own certificate that references the original via supersedes_approval_id. This directly addresses the industry's own guidance that a repeat order on a different garment color needs fresh approval, and no competing product does it.

### 5.7 The chase engine

Approval delay is why shops write "turnaround does not include approval time" into their terms. Printavo's own advice is email plus SMS plus a phone call after 24–48 hours — three channels, because one does not work.

There are two cadences, because customers go quiet at two different moments and the first one is the longer wait.

Intake cadence — the proof is in awaiting_art and no artwork has arrived. Default intake_window_days is 10.

| When | Channel | Content |
|---|---|---|
| T+0 | Email, and SMS if enabled | Send us your logo, with the upload link |
| T+2 days | Email | Nudge, resend link, offer to take a photo of anything they have |
| T+4 days | SMS | One sentence and a link |
| T+6 days | Task to Dana | "Call Marcus — asked for artwork 6 days ago" |
| T+intake_window_days | Email to customer, status to intake_expired | Link closed, one tap to reopen |


Response cadence — the proof has been sent and not answered. Default response_window_days is 7.

| When | Channel | Content |
|---|---|---|
| T+0 | Email, and SMS if enabled | Proof ready, with hero image and link |
| T+2 days, not opened | Email, different subject line | Gentle nudge, resend link |
| T+3 days, not opened | SMS | Short, one sentence and a link |
| T+4 days, opened not answered | Email | "Any questions? Reply here or call us." |
| T+6 days | Task to Dana | "Call Marcus — proof sent 6 days ago, opened twice, no response" |
| T+response_window_days | Email to customer, status to expired | Window closed, one tap to reopen |


Rules that keep this from being obnoxious: quiet hours by contact.timezone (no sends before 8am or after 8pm local); one channel per day maximum; all reminders cancel instantly on any customer action; per-proof snooze via proof.reminders_snoozed_until; and contact.opted_out_at honored across the account. SMS carries STOP/HELP handling, and a STOP sets opted_out_at.

Deadline awareness. When a due date is set and the shop's lead time is known, both cadences compress automatically and the customer-facing copy changes: "To hit your October 3 date we need approval by September 24."

### 5.8 Production release

The moment approval lands:
- Push notification and email to the shop, in a form readable from across a room.
- The production packet unlocks per the account's release_gate_policy: hard withholds the machine file until release, soft allows download with an unapproved banner and a recorded override event, off places no restriction. New accounts default to soft — a hard gate on day one frustrates shops doing repeat work, while the banner teaches the habit.
- The proof moves to the Sewing column on the pipeline board.

The run ticket — one page, printable, readable at arm's length, and accessible by QR code from the board:
- Job reference, customer, due date, and an unmissable APPROVED v2 · Sept 14, 2026 stamp
- Garment style, color, and quantity broken out by size
- Placement with measurements and a diagram
- Hoop size and recommended backing, derived from fabric: cutaway or no-show mesh for knits, tearaway for twill and canvas, water-soluble topping for fleece, terry, corduroy and sweaters, 2.5–3.0 oz for structured caps
- Thread stops in sew order — stop number, brand, code, name, swatch, stitch count. This is the sheet that does not exist today because a DST carries no thread names.
- Total stitches, color changes, trims, and estimated run time per piece and for the run
- Special instructions and any risk overrides carried forward from triage
- File formats included and a download link

### 5.9 Supporting tools

The things that are "normally part of this workflow" and that turn a proofing feature into a product shops rely on daily.

Thread inventory and matching. The shop tells PiperStitch which thread brands and cones it actually owns. Colorway assignment then prefers threads in stock, and flags "you don't have this one" before the proof goes out — a genuinely infuriating failure mode today. Matching uses CIELAB and ΔE2000, not sRGB distance, and always returns the top five candidates with their ΔE values rather than one confident wrong answer. A Pantone code entered by the customer is accepted and stored as a string only, with a recorded caveat, and never converted using a shipped Pantone table; Adobe's 2022 delisting is the precedent and the conversion data is licensed.

Placement library. The defaults in 5.3 are the starting point. Shops save their own — "our standard left chest", "Riverside Little League cap" — and reuse them with one tap. Placements carry size, offsets, hoop and backing.

Estimator. Stitch count is already known, so run time and price follow: stitches ÷ machine speed, plus per-color-change and per-trim time, plus hooping time per piece, across the head count Dana configures. Priced at her own per-thousand rate. Surfaces as "this run is about 2 hours 40 minutes across 24 pieces" on the run ticket, and as an optional price line on the proof. Cap runs automatically use the lower cap speed.

Sew-out log. Every physical test sew gets logged against the proof: photo, garment, backing, needle, thread, speed, and what went wrong. Over time this becomes the shop's own institutional memory — "last time we ran this on a 3XL pique we needed a knockdown" — which is the knowledge that walks out the door when a part-timer quits.

Reorder and repeat approval. Reordering an approved design creates a new proof pre-filled from the last one. If nothing changed, it is approved in one tap by the customer with a much shorter page. If anything changed, conditions-changed detection turns it into a re-confirmation. This is the feature that makes the second year of a customer relationship profitable.

Templates and canned responses. Saved proof messages, saved change-request replies, saved terms, saved intake question sets, per-item defaults.

Pipeline board. A kanban of every live proof by state, with age-in-state, who is waiting, and a color-coded urgency against due date. This is Dana's home screen and the answer to "what do I do next."

Team. Additional seats with three roles: Owner (everything), Stitcher (read proofs, read run tickets, log sew-outs, mark complete), and Sales (create and send proofs, cannot release to production). Internal review stage requires a second seat.

Reporting. Approval rate, median time to approval split into shop time and customer time, revisions per proof, most common change-request reasons, proofs by outcome, and time saved. The change-request reason breakdown is quietly one of the most valuable reports in the product: a shop that learns 40% of its revisions are color will start sending colorways up front.

## Data model

Postgres. account_id on every table. Soft delete via archived_at. All timestamps timestamptz stored UTC, with the actor's offset stored separately where it matters legally.

Owned elsewhere. account and core_project are Core entities and are referenced but not redefined here. contact is defined in Module architecture because it is shared with Clients. Everything else below is new with Proofs.

### Identity and access

user — a person with a login.

id · email · name · password_hash · mfa_secret · last_seen_at · created_at · disabled_at

account_user — membership and role. Seats counted against account_entitlements.seats.

id · account_id · user_id · role (owner, sales, stitcher) · invited_at · accepted_at · disabled_at · counts_against_seats (bool)

Role capabilities, enforced in API middleware: Owner — everything. Sales — create, compose, send, respond to messages, record on-behalf approvals; cannot release to production or change settings. Stitcher — read proofs, read run tickets, log sew-outs, mark complete; cannot send or release.

### Core entities

proof — the job-level container.

id · account_id · contact_id · reference (human-readable, e.g. PF-1042) · title · status (derived rollup — see Two scopes; never hand-set) · current_version_id · core_project_id · due_date · intake_window_days · response_window_days · revisions_included · reminders_snoozed_until · void_reason · created_by (→ user) · created_at · updated_at · archived_at · clients_order_line_id (nullable, set only when Clients is active)

proof_version — immutable once sent.

id · proof_id · version_number · status (authoritative, version-scoped states) · design_hash · stitch_count · color_change_count · trim_count · width_mm · height_mm · fabric_code · hoop_code · garment_template_id · garment_color · garment_style_name · quantity · size_breakdown (jsonb) · placement_id · placement_overrides (jsonb) · terms_version_id · message_body · sew_out_photo_file_id · options (jsonb: density map, animation, price line) · artifact_hashes (jsonb, fixed keys) · pdf_url · render_url · hero_url · composed_by (→ user) · composed_at · internal_reviewed_by (→ user) · internal_reviewed_at · sent_at · response_expires_at · superseded_at

artifact_hashes uses one fixed key set everywhere it appears: {pdf, render, hero, social, machine_files: {dst, pes, exp, jef, vp3}}. approval_record.artifact_hashes uses the identical shape so verification is a direct comparison.

colorway

id · proof_version_id · ordinal (1–4, the value the public API accepts) · name · is_default

thread_stop

id · colorway_id · stop_number · thread_brand · thread_code · thread_name · hex · lab_l · lab_a · lab_b · stitch_count · in_stock_at_send (bool)

file — one table for every uploaded or generated binary. Generalized deliberately: sew-out photos, change-request attachments and message attachments are not artwork and must not be forced through an artwork enum.

id · account_id · proof_id (nullable) · kind (artwork, sew_out_photo, change_attachment, message_attachment, garment_photo, certificate) · source (intake, email, upload, mms, generated) · original_filename · mime_type · bytes · storage_key · sha256 · scan_status · scan_result · converted_from_id · uploaded_at · uploaded_by_contact (bool) · uploaded_by_user_id

triage_report

id · file_id · effective_ppi · is_vector · has_transparency · color_count · colorspace · detected_fonts (jsonb) · generated_at

triage_finding

id · triage_report_id · code · severity (blocker, warning, note) · title · message · measurement (jsonb) · suggested_fix · status (open, resolved, overridden) · overridden_by (→ user) · overridden_reason · overridden_at

### Approval and evidence

access_token — three purposes, three lifetimes.

id · account_id · proof_id · proof_version_id (nullable — null for intake tokens, which exist before any version) · approval_record_id (nullable — set for certificate tokens) · contact_id · token_hash (SHA-256; plaintext never stored) · purpose (intake, proof, reconfirm, certificate, triage_report) · version_scope (current, pinned, all_read_only) · issued_at · expires_at · revoked_at · first_used_at · use_count · max_uses

Lifetimes: intake and proof tokens default to 30 days, extended on activity, revoked on supersede or void. certificate tokens are never revoked and never expire while the approval record is retained — the certificate is promised to both parties for seven years and a 30-day link cannot deliver that.

proof_event — append-only, hash-chained, never updated or deleted. Append-only enforced at the database grant level, not by convention.

id · proof_id · proof_version_id (nullable) · sequence (monotonic per proof) · event_type · occurred_at · local_offset · time_source · actor_type (contact, user, system) · actor_id · token_hash · ip · ip_reverse_dns · user_agent_raw · user_agent_parsed (jsonb) · geo (jsonb, city-level, with geoip_db_version) · payload (jsonb) · prev_event_hash · event_hash

Event types: created, intake_sent, intake_opened, art_uploaded, triage_completed, triage_report_sent, intake_expired, intake_resent, project_linked, composed, internal_review_requested, internal_review_passed, internal_review_changes_requested, sent, delivered, bounced, opened, viewed, zoomed, colorway_selected, question_asked, question_answered, changes_requested, approved, approved_with_notes, declined, reminder_sent, expired, resent, superseded, reconfirmed, released, release_gate_overridden, completed, voided, conditions_changed, override_recorded.

approval_record

id · proof_version_id · colorway_id · approved_at · method (self_service, on_behalf) · on_behalf_channel (phone, in_person, text, email, null) · on_behalf_evidence · recorded_by_user_id · signer_name_typed · signer_email · signer_ip · signer_user_agent · terms_version_id (the terms and consent text in force; terms_version.consent_text is the consent shown) · consent_text_rendered (the exact string displayed, stored verbatim) · artifact_hashes (jsonb, fixed keys) · conditions_snapshot (jsonb: garment_template_id, garment_color, width_mm, height_mm, placement, fabric_code, design_hash, colorway_ordinal, quantity) · certificate_file_id · certificate_sha256 · supersedes_approval_id (for re-confirmations) · notes

change_request

id · proof_version_id · sequence · kind (pin, chip, freetext, attachment) · chip_code · body · pin_x · pin_y (normalized 0–1 against the render, so Core can overlay them on its canvas) · attachment_file_id · created_at · status (open, addressed, declined, discussed) · resolution_note · resolved_in_version_id

message — the question/answer thread.

id · proof_id · proof_version_id · direction · author_type · author_id · body · attachment_file_id · created_at · read_at

### Settings and supporting

shop_settings — one row per account. Everything on screen S11 that is not its own entity.

account_id · shop_name · logo_file_id · brand_color · reply_to_email · phone · address · release_gate_policy (hard, soft, off; default soft) · units (imperial, metric) · default_intake_window_days · default_response_window_days · default_revisions_included · additional_revision_fee · sew_out_fee · quiet_hours_start · quiet_hours_end

terms_version — id · account_id · label · body · consent_text · created_at · is_current. Never edited; changes create a new row.

intake_question_set — id · account_id · name · is_default intake_question — id · intake_question_set_id · sort_order · prompt · field_type (text, number, select, date, size_grid) · options (jsonb) · required · maps_to (garment_style_name, garment_color, quantity, size_breakdown, width_mm, placement_id, due_date, or null for free-form)

message_template — id · account_id · kind (proof_message, change_reply, intake_message) · name · body · is_default

machine_profile — drives the estimator. id · account_id · name · needle_count · head_count · flat_speed_spm · cap_speed_spm · color_change_seconds · trim_seconds · hooping_seconds_per_piece · price_per_thousand_stitches · minimum_charge · is_default

webhook_endpoint — id · account_id · url · secret · events (jsonb array) · enabled · last_success_at · last_failure_at · consecutive_failures

garment_template — id · account_id (null for system templates) · name · category · photo_file_id · displacement_map_key · shading_map_key · available_colors (jsonb) · zones (jsonb: named quads with real-world scale)

placement — id · account_id (null for system) · name · garment_category · default_width_mm · default_height_mm · anchor · offsets (jsonb) · hoop_code · backing_code · notes

thread_chart — id · brand · line · source (measured, user_supplied, imported) · license_note · version thread_color — id · thread_chart_id · code · name · hex · lab_l · lab_a · lab_b · measured_at · measurement_device thread_inventory — id · account_id · thread_color_id · cones_on_hand · updated_at

sew_out — id · account_id · proof_version_id · photo_file_id · garment_note · fabric_code · backing · needle · thread_note · machine_speed · outcome (good, needs_work, failed) · issues · created_by (→ user) · created_at

reminder_schedule — the cadence definition. Seeded per account from defaults, overridable per proof.

id · account_id · proof_id (nullable; null = the account default) · cadence (intake, response) · step_index · offset_hours · channel (email, sms, task) · condition (always, not_opened, opened_no_response) · message_template_id · enabled

reminder_send — the per-send log.

id · reminder_schedule_id · proof_id · proof_version_id (nullable) · channel · to_address · provider_message_id · status (queued, sent, delivered, bounced, failed, suppressed) · suppressed_reason (quiet_hours, opted_out, snoozed, one_per_day) · scheduled_for · sent_at · failed_reason

account_entitlements — account_id · plan_code · proofs_enabled · clients_enabled · seats · sms_enabled · sms_credits · free_proofs_granted (default 3) · free_proofs_used · trial_ends_at

free_proofs_used increments on the first send of a proof, not on creation, so drafts and experiments cost nothing.

## API surface

REST over HTTPS, JSON, versioned at /v1. Three auth realms with entirely separate middleware: session/API-key for shop users, token for the public customer surface, and one unauthenticated content-addressed route for certificate verification.

### Shop API

POST   /v1/proofs                          create; accepts source_proof_id to clone a reorder

GET    /v1/proofs?status=&contact_id=&q=   list (pipeline board)

GET    /v1/proofs/{id}                     detail with versions and events

PATCH  /v1/proofs/{id}                     title, due date, windows, snooze

POST   /v1/proofs/{id}/void                {reason}

PUT    /v1/proofs/{id}/project             link or relink the Core project

POST   /v1/proofs/{id}/intake              send intake link

POST   /v1/proofs/{id}/intake/resend       reopen after intake_expired

POST   /v1/proofs/{id}/files               direct upload (presigned)

GET    /v1/proofs/{id}/files

POST   /v1/files/{id}/triage               re-run triage

PATCH  /v1/triage-findings/{id}            resolve or override

POST   /v1/proofs/{id}/triage-report/send  share findings with the customer

POST   /v1/proofs/{id}/versions            compose a version from a Core project

GET    /v1/proofs/{id}/versions/{n}

POST   /v1/proofs/{id}/versions/{n}/send

POST   /v1/proofs/{id}/versions/{n}/resend                reopen expired or declined

POST   /v1/proofs/{id}/versions/{n}/internal-review       request review

POST   /v1/proofs/{id}/versions/{n}/internal-review/decision  {verdict: pass|changes_needed, note}

GET    /v1/proofs/{id}/versions/{a}/compare/{b}

POST   /v1/proofs/{id}/reconfirm           mint a re-confirmation link after conditions changed

POST   /v1/proofs/{id}/approvals/on-behalf record a phone/in-person approval

GET    /v1/approvals/{id}/certificate      PDF

POST   /v1/approvals/{id}/verify           re-hash artifacts, return verdict

POST   /v1/proofs/{id}/release             unlock production packet

GET    /v1/proofs/{id}/run-ticket          PDF and JSON

GET    /v1/proofs/{id}/machine-files       gated on shop_settings.release_gate_policy

POST   /v1/proofs/{id}/complete

GET    /v1/proofs/{id}/messages

POST   /v1/proofs/{id}/messages            reply to a customer question

PATCH  /v1/change-requests/{id}            status and resolution

GET    /v1/placements     POST /v1/placements     PATCH /v1/placements/{id}

GET    /v1/garment-templates                       POST (custom upload)

GET    /v1/thread-charts  GET /v1/thread-colors?q=&brand=

GET    /v1/thread-inventory                        PUT (bulk set)

POST   /v1/thread-match                            {hex|lab, brand[], in_stock_only} → top 5 with ΔE00

POST   /v1/estimate                                {stitches, color_changes, trims, qty, machine_profile_id} → time and price

POST   /v1/sew-outs

GET    /v1/sew-outs?proof_id=&fabric_code=&outcome=        the retrospective the feature exists for

GET    /v1/settings       PATCH /v1/settings

GET    /v1/terms-versions  POST /v1/terms-versions

GET    /v1/intake-question-sets  POST  PATCH

GET    /v1/machine-profiles      POST  PATCH

GET    /v1/message-templates     POST  PATCH

GET    /v1/webhook-endpoints     POST  DELETE

GET    /v1/team           POST /v1/team/invite   PATCH /v1/team/{account_user_id}

GET    /v1/reports/proof-metrics?from=&to=

### Inbound provider webhooks

POST   /v1/inbound/postmark    delivery, open, bounce, spam-complaint events

POST   /v1/inbound/telnyx      delivery receipts, inbound SMS, STOP/HELP

POST   /v1/inbound/email       parsed forwarded mail to art@{shop-slug}.piperstitch.com

All three verify the provider's signature before processing. The email route additionally enforces the sender rules in Art intake.

### Public token API

Every route takes the opaque token in the path. No database identifier is accepted or returned anywhere in this realm — colorways are addressed by ordinal (1–4) resolved against the token's own version, and no route accepts an account id.

GET    /p/{token}                          proof page payload

GET    /p/{token}/versions                 list, read-only

GET    /p/{token}/versions/{n}             an earlier version, read-only, no actions

POST   /p/{token}/view                     record view, dwell, zoom

POST   /p/{token}/approve                  {colorway_ordinal, signer_name, consent_version,

consent_accepted, notes?}  — notes present ⇒ approved_with_notes

POST   /p/{token}/changes                  {items:[{kind, chip_code, body, pin_x, pin_y}]}

POST   /p/{token}/question                 {body}

POST   /p/{token}/decline                  {reason}

GET    /p/{token}/pdf                      proof only; no certificate appended

POST   /p/{token}/sew-out-request

POST   /p/{token}/new-link                 emails a fresh link to the contact on file

GET    /i/{token}                          intake form schema

POST   /i/{token}/files                    presigned upload grant

POST   /i/{token}/submit                   answers

GET    /r/{token}                          re-confirmation page (changed conditions only)

POST   /r/{token}/confirm

GET    /t/{token}                          readiness report, read-only

GET    /c/{token}                          the approval certificate, for the signer

Certificate access is deliberately narrow. A certificate contains the signer's typed name, email, IP address, user agent and coarse location. It is served only through the certificate token minted for that signer at approval, never through a proof token — otherwise, on a multi-recipient send, recipient B downloads recipient A's IP address. GET /p/{token}/pdf returns the proof without the certificate appended. A co-recipient who needs the record gets a redacted copy from the shop, with IP, user agent and geolocation removed.

### Verification

GET    /verify/{certificate_sha256}

Unauthenticated and content-addressed: the caller must already hold the certificate's hash, so nothing is enumerable. Returns pass or fail, the artifact list with each hash's current verdict, and the shop's name. This is the URL printed on the certificate, so the party the certificate protects can actually use it.

### Outbound webhooks

Signed with HMAC-SHA256 over the raw body, X-PiperStitch-Signature, five-minute timestamp tolerance, at-least-once delivery with exponential backoff to 24 hours, and an idempotency key per event.

proof.created · proof.intake_sent · proof.art_received · proof.sent · proof.viewed · proof.changes_requested · proof.approved · proof.approved_with_notes · proof.declined · proof.expired · proof.superseded · proof.released · proof.completed · proof.voided · triage.completed · approval.reconfirmed

A Zapier/Make integration built on these covers the long tail of shop workflows without building point integrations.

## Technical architecture

### Services

| Service | Responsibility | Stack |
|---|---|---|
| Web app | Shop UI and public proof pages | Existing PiperStitch stack |
| API | Business logic, state machine, entitlements | Existing |
| Stitch service | Parse, analyze, render, convert embroidery files | Python — this is non-negotiable |
| Render service | Garment compositing, PDF generation | Python + pyvips/ImageMagick + headless Chromium |
| Intake worker | AV scan, format conversion, image analysis, triage | Python |
| Notification worker | Email, SMS, push, reminder cadence | Existing stack + provider SDKs |


Why Python for stitch handling: there is no production-grade embroidery file library for Node, Go or Rust. The npm package named embroidery is an unrelated event library; the Rust crate oxideav-embroidery is at 0.0.3 and does not read VP3. The mature option is pystitch (MIT, v1.0.1, June 2026, maintained by the Ink/Stitch team) — reads 46 formats, writes 11, full command set, and reads and writes the .col, .edr and .inf color sidecars. Put it behind an HTTP boundary and do not attempt a native reimplementation.

### The stitch render pipeline

Four fidelity tiers, and the product uses three of them for different jobs:
- Polyline SVG — one stroked path per stitch run at ~0.4 mm round cap. Fast, resolution-independent, used for the density map, the stitch-order animation and internal QC.
- Instanced textured quads — the customer-facing render. One quad per stitch, oriented along the stitch vector, tiled thread albedo plus normal map, with anisotropic specular for filament sheen and a 0.5–1 mm ambient-occlusion drop shadow so the embroidery sits proud of the cloth. In the browser this is THREE.InstancedMesh; a 5,000–40,000-stitch commercial logo is comfortable on any modern GPU.
- Server-side deterministic render — the same scene in headless Chromium with --use-angle=swiftshader on GPU-less nodes, screenshotted at specified pixel dimensions. Determinism matters: the PDF, the web page and the email image must agree, and the hash must be stable.

Garment compositing: garment photo → blurred luminance as displacement map → warp the render with pyvips mapim (Sharp has no displacement operation; do not plan on it) or ImageMagick -compose Displace → four-point perspective distort for curved zones such as sleeves and caps → multiply against the shading layer → screen the highlights → composite the AO drop.

PDF: headless Chromium via Playwright for the customer-facing RGB proof — best CSS fidelity, ~13 ms warm for a complex page. Specs, measurements and thread codes must be real selectable text, never raster. wkhtmltopdf is unmaintained since 2023 and is not an option. No HTML engine emits CMYK or PDF/X; if a shop ever needs print-production output, post-process with Ghostscript.

### Thread charts — a build-it-yourself problem

No manufacturer publishes an officially licensed machine-readable chart. Madeira, Isacord, Robison-Anton, Sulky, Gunold, Coats, Floriani and Marathon all ship PDFs and physical shade cards only. pystitch's built-in thread catalogs are machine palettes (Brother PEC's 64, Janome JEF, Husqvarna HUS) — not shade cards. Wilcom's own API documentation says plainly that it does not provide thread charts.

The defensible path, and the recommendation:

| Measure the physical shade cards with a spectrophotometer (X-Rite i1Pro or a Nix), key the measured sRGB and Lab values to the manufacturer's own codes, and ship PiperStitch's measured data with a clear "these are our measurements, not a color match — request a sew-out for critical color" disclaimer. Under Feist, a thread code paired with a color value is an uncopyrightable fact; a wholesale copy of a full chart can still implicate compilation copyright, and the EU database right applies there. Brand names are trademarks, so "Madeira Polyneon 1922" is nominative fair use but implied endorsement is not. Measuring your own values sidesteps all of it and produces better data than scraping PDFs. Budget: a Nix Spectro 2 and roughly two days of labour per major shade card. Do the three brands that matter first — Madeira Polyneon, Isacord, Robison-Anton — and let shops import their own CSV for the rest. |
|---|


Color matching runs sRGB → linear → XYZ(D65) → CIELAB → ΔE2000, using colour-science (BSD-3, actively maintained). colormath is effectively unmaintained. Never sRGB Euclidean distance: sRGB is gamma-encoded and perceptually non-uniform, over-weights blue and compresses green, and will confidently return the wrong thread. Always return five candidates with their ΔE values. Metamerism cannot be predicted — no thread vendor publishes spectral reflectance — so the UI states that and does not pretend.

### File intake pipeline

Hard limit: 400 MB per file, 2 GB per intake submission. Presigned direct-to-storage multipart upload into a quarantine bucket; a 400 MB PSD never passes through an app server. Then: ClamAV scan via a clamd sidecar with MaxFileSize/MaxScanSize raised well above defaults and freshclam running against a private mirror every four hours → magic-byte validation against the claimed extension → format conversion (HEIC via pillow-heif, PSD flatten, PDF page selection) → EXIF orientation → ICC handling, converting Display P3 to sRGB rather than discarding the profile, because dropping it shifts colors and this is a color product → analysis → triage → promote to the durable bucket.

Two traps worth naming in the build: never run ImageMagick on user uploads without a restrictive policy.xml disabling the MSL, MVG, EPHEMERAL and HTTPS delegates; and if background removal is used, rembg's default bria-rmbg model requires a paid commercial agreement — use u2net or isnet-general-use (Apache-2.0). remove.bg shuts down its standalone service on 1 December 2026 and must not be built on.

### Environments and data residency

US-region primary. Versioned object storage with object lock on everything referenced by an approval record. Nightly logical backups with a 30-day point-in-time recovery window. The proof_event table is append-only at the database grant level, not merely by convention.

## Notifications

### Matrix

| Trigger | To customer | To shop | Channel |
|---|---|---|---|
| Intake link sent | Upload your artwork | — | Email, SMS |
| Intake reminders | Per 5.7 intake cadence | — | Email, SMS |
| Intake expired | Link closed, tap to reopen | Never sent us artwork — call them | Email / push |
| Artwork received | Receipt confirmation | New artwork from Marcus | Email / push |
| Unknown-sender email quarantined | — | 3 emails waiting to be accepted | In-app |
| Triage found a blocker | — | Art problem found before you start | Push, in-app |
| Readiness report shared | Here's what we found in your file | — | Email |
| Internal review requested | — | Reviewer: a proof is waiting for you | Push, in-app |
| Internal review decided | — | Composer: passed, or changes needed with the note | Push, in-app |
| Proof sent | Your proof is ready | — | Email, SMS |
| Proof delivered | — | Delivered | In-app only |
| Proof opened | — | Marcus opened your proof | Push (throttled to first open) |
| Question asked | — | New question | Push, email |
| Shop replied | You have a reply | — | Email, SMS |
| Changes requested | Receipt confirmation | Changes requested — 3 items | Push, email |
| New version sent | Updated proof, here's what changed | — | Email, SMS |
| Version superseded | To any other recipient whose link was revoked: a newer version is out, here it is | — | Email |
| Approved | Confirmation and certificate link | Approved — cleared to sew | Push, email, SMS to shop |
| Declined | Receipt | Declined, with reason | Push, email |
| Response reminders | Per 5.7 response cadence | — | Email, SMS |
| Expiring in 24h | Last chance | Heads up | Email / in-app |
| Response window expired | Window closed, tap to reopen | Proof expired — call them | Email / push |
| Released to production | — | Ray: new job on the board | Push, email |
| Job completed | Your order is finished | Job closed | Email / in-app |
| Proof voided | This request has been cancelled by the shop | Confirmation | Email / in-app |
| Conditions changed | Quick re-confirm | — | Email, SMS |
| Delivery bounced | — | Email bounced — check the address | Push, in-app banner |


Two rows deserve emphasis. A proof sitting in sent because the email hard-bounced looks identical to a customer ignoring it, and shops lose days to this — bounces must raise a loud, specific alert. And a customer whose link was revoked by a supersede or a void must be told why; otherwise they hit the expired-link page mid-approval with no explanation.

### Providers

Email: Postmark for everything approval-related. Higher per-message cost (~$0.0018 vs Resend's \\\\\\\~$0.0004) buys measurably better inbox placement because transactional and broadcast run on separated infrastructure, and its searchable activity log doubles as corroborating evidence in a dispute. At realistic volumes this is single-digit dollars a month. Use SES for bulk and marketing.

SMS: Telnyx at $0.004 per segment against Twilio's $0.0083, with identical carrier pass-through (T-Mobile $0.003, AT&T $0.003, Verizon $0.0045).

| A2P 10DLC is the biggest schedule risk in this project. Registration work must start in week one of Phase 2 — one full phase before SMS ships in Phase 3 — because one to three weeks of carrier review per shop cannot be compressed. Sending on behalf of customers means registering as an ISV: a Primary Customer Profile for PiperStitch, then a Secondary Customer Profile per shop, each with its own brand ($4.50 one\\\\\\\-time) and campaign ($15 one-time vetting, $10 per month, recurring). Aggregating unrelated shops under one campaign is a carrier violation, not a gray area. That recurring $10 is a per-subscriber cost of goods and sets the floor for what the SMS add-on can be priced at — see Packaging and pricing. Sole proprietor registration is unusable for this product: 1 message per second, 3,000 segments per day total, 1,000 per day to T-Mobile. Standard brands with a good trust score get 120–225 MPS and 50–200k per day. Vague use-case descriptions are the top rejection cause — write the sample messages and opt-in evidence carefully. Opt-in evidence comes from the intake form, where the phone number is collected with explicit consent language and stored on contact.sms_opt_in_at and sms_opt_in_text. Therefore: ship email-first. SMS is a paid add-on in Phase 3, gated behind per-shop registration that PiperStitch runs on the shop's behalf during onboarding. |
|---|


## Security, privacy and compliance

Token design. 256 bits of entropy, base62-encoded, URL-safe. Only the SHA-256 is stored. Rate-limited per token and per IP. A revoked or expired token returns a friendly page with a "request a new link" action that emails the contact on file — never an error, and never a way to discover whether a proof exists.

Two clocks, deliberately separate. proof_version.response_expires_at is the business clock: it drives the expire transition and the chase cadence, and defaults to 7 days. access_token.expires_at is the access clock: it defaults to 30 days, extends on activity, and intentionally outlives the response window so that an expired proof still opens, still shows what was sent, and still offers a fresh link. Certificate tokens have no expiry at all while the approval record is retained. Nothing in the state machine reads access_token.expires_at.

Enumeration. No sequential identifiers anywhere in the public surface; colorways are addressed by ordinal, not id. The public API never accepts an account id. The verification endpoint is content-addressed by certificate hash, so a caller must already hold the certificate to use it. Proof references (PF-1042) are for humans and appear only inside authenticated surfaces and on the proof page itself.

Access control. Role-based against account_user.role, enforced in API middleware and never only in the UI. Sales cannot release to production; Stitcher cannot send. The internal-review guard checks that the reviewer is not proof_version.composed_by.

Evidence integrity. proof_event is append-only at the grant level. Each row carries prev_event_hash, making silent insertion or deletion detectable by replaying the chain. A daily Merkle root over each account's event chain is computed and stored separately; optionally anchored with an RFC 3161 timestamp token from a public TSA. This is cheap and turns "we have logs" into "we have tamper-evident logs."

PII. Customer contact data is the shop's, not PiperStitch's. Support a per-contact export and a per-contact deletion that redacts identifying fields from proof_event while preserving the chain's hash integrity by hashing the redacted values — the chain must still verify after redaction, which means designing for it now rather than retrofitting.

| The certificate is exempt from contact deletion. A Certificate of Approval is a record of a completed commercial transaction, held under object lock for seven years, and it necessarily contains the signer's name, email, IP and user agent — redacting it would destroy the thing it exists to prove. A deletion request suppresses the contact everywhere else, and the certificate is retained on the legitimate-interest and legal-obligation basis common to executed contracts. The consent text tells the customer this at the moment they approve, and this mechanism is on the pre-launch legal review list. |
|---|


Retention.

| Data | Retention |
|---|---|
| Approval records and certificates | 7 years minimum; never deleted by a subscription lapse |
| Proof version artifacts (PDF, render, hero, social, machine files) | Life of the approval record where one exists — 7 years. Otherwise account-active + 90 days |
| Artwork and intake files with no approval | Account-active + 90 days |
| Event chains | Life of the account |
| Reminder send logs | 2 years |


The artifact row is load-bearing: POST /v1/approvals/{id}/verify and /verify/{hash} re-hash exactly those files, and Invariant 4 requires them to still verify. If machine files fell under the 90-day artwork rule, every certificate would become unverifiable 90 days after cancellation — which is precisely when a dispute is most likely.

Uploads. Covered in Architecture. Additionally: a strict Content-Security-Policy on the public proof page, no third-party scripts of any kind on it, and SVG uploads sanitized before any render.

Legal review needed before launch on four items: the default terms and consent text in Appendix A; the thread-chart licensing position; the on-behalf-of approval mechanism; and the certificate retention exemption above.

## Integration with PiperStitch Core

### Where Proofs appears inside Core
- Project screen — a persistent "Send for approval" action in the header. The most important integration point in the product; it is the moment the thought occurs.
- Export dialog — when the account's release_gate_policy is hard or soft and the design belongs to an unapproved proof, exporting shows "This design hasn't been approved yet" with options to send a proof, or to export anyway. hard blocks; soft allows and writes a release_gate_overridden event.
- Global nav — Proofs and the pipeline board as a first-class section.
- Home — a compact "waiting on customers" strip above the project list.
- Colorways panel — built in Core, consumed by Proofs.

### Data flow from Core to Proofs

Composing a version calls into Core for: design_hash, stitch count, color-change and trim counts, bounding dimensions, the ordered stop list with the project's palette, the fabric and hoop settings, and a render at specified dimensions. Proofs copies all of it onto proof_version and never reads through to the live project when displaying a sent proof. The project can change freely; the sent proof cannot.

How a later change is noticed. Proofs does not poll and does not read through. Core emits project.design_changed — the internal event listed in Module architecture — whenever a project's design_hash changes. Proofs subscribes, looks up any approval_record whose conditions_snapshot.design_hash matches the old value, and fires conditions_changed on those. Shop-side edits to garment, color, size, placement or quantity are Proofs' own writes and trigger the same check inline.

| design_hash pre-image, defined exactly. Getting this wrong makes the feature either useless or infuriating. Included: the ordered stitch blocks (coordinates in 0.1 mm), the stop boundaries and their order, the trim and jump commands, and the finished bounding dimensions. Excluded: thread palette and colorway assignments, colorway names, project name, view and zoom settings, editor metadata, and file-format selection. Palette changes are therefore not design changes — they are caught separately, because conditions_snapshot stores colorway_ordinal and the thread stops behind it. If thread assignments were in the pre-image, every colorway experiment would fire a false alarm; if colorways were not tracked at all, a thread swap would slip through. Both are covered, by different mechanisms. |
|---|


### Round-tripping change requests

Opening a proof's change list from within Core shows the pins overlaid on the design canvas, positioned by the same normalized 0–1 coordinates the customer tapped. Dana works down the list and checks items off inside the editor. This is a small feature that will feel like magic.

### Graceful degradation

The rule, stated precisely: no link a customer holds ever 404s or errors, and any approval already in flight can still be completed. A lapsed subscription stops new proofs going out; it does not strand a customer mid-approval, does not hide a shop's own data, and does not break a certificate link. Transactional link-recovery email stays exempt from the reminder freeze. The full behavior is in Module architecture.

## Screen specification

### Shop surfaces

S1 · Pipeline board. Seven columns: Needs attention · Waiting on Art · Digitizing · Out for Approval · Changes Requested · Approved · Sewing.

Needs attention is the first column for a reason — it holds expired, intake_expired, declined, bounced-delivery proofs, quarantined unknown-sender emails, and proofs with an open conditions-changed flag. These are the proofs Dana most needs to act on and they would otherwise be invisible. draft proofs live in a collapsed tray at the top of the first column. completed, void and superseded are excluded from the board and reachable by filter.

Cards show customer, thumbnail, version, age in state, due-date urgency bar, and a one-tap primary action. Filters by contact, due date, and state. This is the home screen; it must load in under a second and work one-handed on a phone.

S2 · New proof. Contact (search or create inline), title, due date, then a fork: send an intake link, upload art now, start from an existing project, or reorder from a previous proof (which clones garment, placement, colorway and terms). Three taps to sent.

S3 · Intake composer. Which question set to use, message text, channel, intake window. Preview of exactly what the customer will see.

S4 · Art Readiness Report. Thumbnail, file facts, then findings as cards grouped by severity, each with the measurement, the plain-English explanation, the suggested fix, and Resolve / Override / Ask the customer. A "send this to the customer" action at the bottom, which mints the read-only report link.

S5 · Proof builder. Left: the composed proof preview exactly as the customer will see it, scrollable. Right: panels for garment, placement, colorways, options, message, terms, response window. Everything pre-filled. Live preview on every change. A prominent Send, a quieter Save draft, and Request internal review when the account has more than one seat.

S6 · Send dialog. Recipients (multiple allowed, each gets a distinct token), channels, schedule-for-later, reminder cadence override, and a final summary of what is being approved.

S7 · Proof detail. Header with state, version selector, and the primary action. Tabs: Proof (the rendered artifact) · Changes (the checklist) · Messages (the thread) · Activity (the full event timeline, human-readable) · Files. The activity timeline should read like a story, not a log: "Marcus opened the proof on his iPhone at 7:42pm and looked at it for 3 minutes."

S8 · Version compare. Side-by-side with synchronized zoom, a swipe slider, and a difference view. The change list from the earlier version shown alongside, with resolutions.

S9 · Approval certificate viewer. The certificate rendered in-app with a Verify button that re-hashes the artifacts and reports pass or fail, plus download, email, and "send a redacted copy" actions.

S10 · Run ticket. Print-optimized, large type, QR code linking back to the proof. Also the screen Ray opens on a tablet at the machine.

S11 · Settings. Brand (logo, colors, reply-to, shop name and contact details) · Terms versions · Intake question sets · Reminder cadences, both intake and response · Release gate policy · Placements · Garment templates · Thread brands and inventory · Machine profiles and estimator rates · Message templates · Team and roles · Webhooks · SMS.

S12 · Reports. The metrics in 5.9, as a small number of well-chosen charts rather than a dashboard nobody reads.

S13 · Email inbox. The quarantine queue for unknown-sender forwarded mail: sender, subject, attachment thumbnails, Accept or Discard.

### Customer surfaces

C1 · Intake form. One column, large targets, progress indicator, camera-first upload on mobile, saves as you go so a dropped connection loses nothing.

C2 · Proof page. Specified in detail in 5.4.

C3 · Approve confirm sheet. Exactly what is being approved, consent checkbox, typed name, Approve.

C4 · Change request surface. Pin-and-comment on the render, quick-tap chips, free text, attachments, and a review step before submitting.

C5 · Confirmation. Warm, brief, with the certificate download and a clear statement of what happens next and by when.

C6 · Re-confirmation page. Only what changed, side by side with what was originally approved. One button.

C7 · Expired or revoked link. Friendly, with a request-a-new-link action. Never an error page.

## Non-functional requirements

| Area | Requirement |
|---|---|
| Proof page load | Under 1.5 s to first contentful paint on 4G; under 300 KB above the fold |
| Proof composition | Under 8 s from tap to preview for a 25,000-stitch design |
| PDF generation | Under 5 s |
| Triage | Under 20 s at 50 MB, under 90 s at the 400 MB file limit, with progress shown throughout |
| Upload | 400 MB per file, 2 GB per submission, resumable, direct to storage |
| Pipeline board | Under 1 s with 500 active proofs |
| Availability | 99.9% for the public proof surface, which is customer-facing and load-bearing |
| Email delivery | 99%+ delivered; bounces surfaced within 60 s |
| Browser support | Last two versions of Safari, Chrome, Firefox, Edge; iOS 16+; Android 10+ |
| Accessibility | WCAG 2.2 AA on every customer-facing surface, no exceptions |
| Localization | English at launch; all customer-facing strings externalized from day one; units switchable between inches and millimetres per account |
| Offline | Run ticket cached and readable without connectivity |


## Packaging and pricing

### Recommendation

| Plan | Price | Contents |
|---|---|---|
| PiperStitch | $24/mo | Core, unchanged |
| PiperStitch + Proofs | $49/mo | Core plus the full Proofs module, unlimited proofs, 1 seat |
| Proofs add-on for existing subscribers | +$25/mo | Same, added to an existing $24 plan |
| Annual | $490/yr | Two months free; the standard SMB lever |
| Extra seat | +$9/mo | Stitcher or Sales role |
| SMS | +$19/mo | Includes 250 segments, plus 10DLC brand and campaign registration handled for the shop; $0.02/segment after |
| Sew-out logging, estimator, placements, certificates | included | Never a separate SKU |
| PiperStitch Complete (future) | $79/mo | Core + Proofs + Clients |


### Why these numbers

$25 is the right add-on price, and $99 is not. The evidence is consistent: GraphicsFlow's own à-la-carte seat is $10/month; Printavo's extra users are $19; shopVOX's are $29; ReviewStudio Pro is $15/user; GoVisually Lite is $16/user; Dynamic Mockups Pro is $15–19. The apparel-native approval products start at $99 (GraphicsFlow) and $112 (ProofStuff) and are aimed at shops running Printavo at $109–244 on top. That is not PiperStitch's customer.

SMS must be $19, not $9. Every SMS shop carries a recurring $10/month 10DLC campaign fee, plus one-time $4.50 brand and $15 vetting fees, plus roughly $1.75–2.13 for 250 segments at Telnyx plus carrier pass-through. A $9 add-on would lose about $3 per subscriber per month and lose more as adoption grew. $19 covers the campaign fee and the included segments and amortizes the one-time fees within four months. The alternative — passing the $10 through as a visible line item and charging $9 for the service — is honest but adds a second number to a pricing page that should stay simple.

Unlimited proofs, not metered. ShopWorks meters ProofStuff at 1,000/2,000/3,000 proofs. Metering is right for a shop doing volume and wrong for a solo operator, for whom a per-proof counter creates hesitation at exactly the moment you want them sending more proofs. Seats and SMS are the expansion levers instead.

$49 bundled is the number to lead with. It roughly doubles ARPU, sits comfortably under every apparel-native alternative, and is under the psychological $50 line. Present $49 as the default plan on the pricing page and $24 as the entry tier, not the other way round.

Do not gate the audit trail. Ziflow paywalls audit-trail retention at 90 days, 1 year and lifetime, and every proofing tool puts e-signature-grade approval above $250/month. PiperStitch should give the full certificate, forever, at $25. It is the single most defensible differentiator and metering it would squander that advantage for a few dollars a month.

### Trial and conversion

Proofs joins the existing 14-day trial. Additionally: every Core subscriber gets three free proofs, ever — three total, no time limit, tracked as free_proofs_granted and free_proofs_used on account_entitlements and decremented on the first send of a proof rather than on creation, so drafts cost nothing. Three per month would be enough for a small shop to never upgrade; three total is enough to feel the product and not enough to run a business on.

The moment a shop experiences a customer approving on a phone and a certificate landing in their inbox, the product has sold itself.

Conversion surfaces: the "Send for approval" button on every project screen; the export-gate dialog; and an in-app sample proof the shop can send to their own email address and click through as the customer.

## Build plan

Four phases. Each ships something a shop can use; none is a prerequisite that produces nothing on its own.

### Phase 1 · The spine (6–8 weeks)

Prove the core loop end to end, with the two things nobody else has.

Scope: contact · user and account_user with the three roles · proof and proof_version with version-scoped status · state machine through approved and released · compose from a Core project · server-side render service · basic garment compositing with 12 templates · proof PDF · the three token purposes (proof, certificate, and the revocation semantics) · the customer proof page with approve, request changes and ask a question · proof_event hash chain · approval_record · Certificate of Approval · content-addressed verification endpoint · shop_settings with release_gate_policy · email via Postmark with inbound bounce handling · pipeline board with the Needs-attention column · run ticket · entitlements, free-proof counter and the $49 bundle.

Deliberately out: intake links, triage, version compare, colorways, SMS, reminders, sew-out log, estimator, thread inventory, internal review.

| Phase 1 acceptance criteria A shop composes a proof from a Core project in under 60 seconds and sends it. The customer opens it on a phone, zooms the render to inspect 4 mm text, and approves without creating an account. The approval writes a certificate containing the SHA-256 of the proof PDF and every machine file, the consent text as displayed, the IP, the user agent, and a verifiable event chain. GET /verify/{certificate_sha256} returns pass when called by an unauthenticated client holding the hash; mutating one byte of the stored PDF makes it return fail. A second recipient on the same send cannot reach the first recipient's certificate, and GET /p/{token}/pdf contains no signer PII. Releasing produces a run ticket with the ordered thread stop list, thread codes, hoop, backing, placement measurements, quantity by size and stitch count. Sending v2 fires supersede on v1 and revokes v1's action tokens in the same transaction; v1 remains readable through GET /p/{token}/versions/{n}. A Sales-role user cannot call POST /v1/proofs/{id}/release; a Stitcher cannot call send. Disabling the Proofs entitlement leaves every existing proof page live, lets an in-flight approval complete, keeps every certificate downloadable, and blocks new sends. The proof page scores WCAG 2.2 AA in automated and manual audit. |
|---|


### Phase 2 · Intake and iteration (5–7 weeks)

Close the front of the funnel and make revision rounds cheap.

Scope: intake links, intake_question_set, and the public intake form · email ingest at art@{shop}.piperstitch.com with DKIM enforcement and the quarantine queue · the full upload and AV pipeline · Art Readiness Report with every check in 5.2 · shareable report link · blocker gating with override · change-request checklist and round-tripping into Core · version compare (side-by-side, slider, difference) · "what changed" summary on new versions · colorways · both reminder cadences and the chase engine (email only) · intake and response expiry, and resend · message threads · internal review stage · bounce alerting.

Also in Phase 2, off the critical path of the code: start A2P 10DLC ISV registration. It cannot be compressed later.

| Phase 2 acceptance criteria A customer uploads a 12 MP phone photo of a business card; triage reports the effective PPI at the requested size, detects the white background, counts colors, and flags text under 5 mm with its measurement in millimetres. Forwarding a customer email from a shop user's own address creates a proof in art_received with attachments extracted, the body preserved, and signature images filtered out. A message with a spoofed From header that fails DKIM is rejected outright, and a DKIM-valid message from an unknown sender lands in quarantine rather than creating a proof. A blocker finding prevents composition until resolved or explicitly overridden, and the override appears on the proof and in the event chain. Change requests land as a checklist with pins; opening the proof inside Core overlays those pins on the canvas at the right normalized coordinates. Version compare renders v1 against v2 with a working difference view on a phone. Both cadences fire on schedule, respect quiet hours in contact.timezone, suppress correctly for opt-out and snooze, and cancel on any customer action within 60 seconds. An internal reviewer can fail a proof back to digitizing with a note, and cannot review a version they composed. A hard bounce raises an in-app banner and a push within 60 seconds. |
|---|


### Phase 3 · The shop's daily tools (5–7 weeks)

The things that make a shop live in the product rather than visit it.

Scope: thread charts (three measured brands) and matching by ΔE2000 · thread inventory and in-stock preference · placement library with the system defaults · custom garment template upload with zone editing · machine_profile and the estimator · sew-out log with retrospective search · reorder and repeat approval · conditions-changed detection subscribing to project.design_changed, and the re-confirmation flow · on-behalf-of approval · SMS on completed 10DLC registrations · MMS intake · extra seats · reporting · outbound webhooks and a Zapier integration.

| Phase 3 acceptance criteria Entering a hex or picking from artwork returns the five nearest threads in the shop's chosen brands with ΔE00 values, and marks which are in stock. Changing a garment color on an approved proof raises the conditions-changed banner and produces a re-confirmation page showing only the delta; confirming writes a certificate that references the original. Editing the design in Core after approval fires project.design_changed, and Proofs flags the approval — while changing only the colorway does not fire a false positive. A phone approval recorded on behalf of a customer produces a certificate that says so, names the channel, and stamps the recording user. A shop completes 10DLC registration through onboarding without leaving PiperStitch, and the first SMS delivers. The estimator's run-time prediction lands within 15% of actual on a logged run, using cap speed for cap jobs. A reorder with no changes is approved by the customer in two taps. |
|---|


### Phase 4 · Polish and scale (ongoing)

Stitch-order animation · density map overlay · advanced garment library · saved proof templates · customer-facing sew-out requests · multi-recipient approval routing (two approvers, sequential or parallel) · white-label domain · deeper reporting · localization · and the seam work that lands PiperStitch Clients. Items 2–4 of Open questions decide what actually goes here.

### Sequencing note

Two items have real-world lead times and must start earlier than the phase that consumes them.

Thread chart measurement is on the critical path for Phase 3 and requires buying a spectrophotometer and shade cards plus roughly two days of measurement per brand. Start during Phase 1.

A2P 10DLC ISV registration is on the critical path for Phase 3 SMS and takes one to three weeks of carrier review per shop, which cannot be compressed. Start in week one of Phase 2.

## Success metrics

Product health
- Median time from proof sent to customer response — target under 24 hours, against an industry norm measured in days
- Proof open rate within 24 hours — target 75%
- Approval rate on first version — target 55%, and rising as colorways and triage do their work
- Revisions per approved proof — target under 3, against an industry average of 7
- Percentage of approvals captured self-service rather than on-behalf — target 80%

Business
- Attach rate of Proofs among Core subscribers — target 35% within 12 months
- Trial-to-paid on the bundled $49 plan versus the $24 plan
- Churn differential: shops on Proofs should churn materially less than Core-only, because the product holds their customer relationships
- Proofs sent per active shop per month — the single best leading indicator of retention

Qualitative signal to watch for. The moment a shop forwards a PiperStitch proof link to a customer instead of attaching a JPG to an email, the product has won. Instrument that: proofs sent per shop in week one versus week eight.

## Risks and open questions

| Risk | Severity | Mitigation |
|---|---|---|
| Render quality below expectations makes the core claim hollow | High | Phase 1 cannot ship until the render survives a side-by-side against a real sew-out with ten shop owners. This is the product; budget time for it. |
| A too-perfect render creates disputes it was meant to prevent | High | Principle 2. Visible stitch texture, non-removable honesty note, prominent sew-out upload. Resist every request to make it prettier. |
| Thread chart licensing | Medium | Measure rather than copy. Legal review before launch. Shop-supplied CSV import as a fallback for any brand not measured. |
| 10DLC registration friction blocks SMS | Medium | Email-first. SMS as a Phase 3 paid add-on. Start ISV registration in week one of Phase 2. |
| Customers still reply by text and phone | High — and certain | This is not a bug to design out. On-behalf-of approval, email ingest, and the message thread all exist because of it. |
| Email ingest abused by a spoofed sender | Medium | DKIM required, DMARC failures rejected, unknown senders quarantined rather than auto-created, per-address rate limits. Tested explicitly in Phase 2. |
| Garment template library too small to be useful | Medium | 40 at launch, custom upload from day one, and a "request a garment" queue that feeds the roadmap. |
| Price anchoring against GraphicsFlow at $99 | Low | $25 is deliberately below the consideration threshold. Do not raise it to look serious. |
| Scope creep into Clients territory | Medium | The contact boundary in Module architecture is the line. Orders, quotes, invoices and payment are Clients; what was approved is Proofs. |


### Decisions taken in this revision

Four questions that were open in the first draft are now closed in the spec, recorded here so the reasoning is not lost:
- Release gate default is soft. A hard gate on day one frustrates shops doing repeat work; a banner plus a recorded override teaches the habit without blocking anyone. Configurable to hard or off.
- Free proofs: three total, ever. Three per month is a free tier in disguise.
- SMS priced at $19. It carries a real recurring carrier cost; see Packaging and pricing.
- Certificates are exempt from contact deletion, as records of a completed transaction. On the legal review list.

### Open questions for Ashley
- Naming. "PiperStitch Proofs" is clear and searchable. Given the bird-and-helper thread in the brand, alternatives worth considering for the module pair are Proofs and Nest (the customer-management module), or keeping both purely descriptive. Descriptive is recommended for the paid add-on — shops search for "proofing," not for "nest."
- Sew-out requests. Should the customer be able to request a physical sew-out from the proof page, with a shop-set fee? It converts a common phone call into a priced service, but collecting the fee needs the payment rail that lives in Clients. Ship the request in Phase 4 and the payment with Clients, or defer both?
- Multi-approver routing. Corporate customers frequently need two sign-offs, sequential or parallel. Worth building in Phase 4, or a distraction from the solo-shop focus?
- White-label domain. proofs.dananeedles.com instead of a PiperStitch URL is a classic upsell lever. Free on the $49 plan, or a reason for a higher tier later?

## Appendix A · Default terms and consent text

Supplied as the shop's editable default. Requires review by counsel before launch. Drawn from published industry norms — placement tolerance, spoilage allowance, color-match language and digitizing-fee terms all follow common practice in decorated apparel.

### Approval terms (default)

Artwork and approval. You are responsible for reviewing and approving the artwork, dimensions, thread colors, garment selection and placement shown on this proof before production begins. Production starts only after approval is received.

Color. Exact color matching is not guaranteed. Thread colors are matched as closely as reasonably possible using available thread inventories. Screen colors vary by device and lighting and are not a color standard. If exact color is critical, request a physical sew-out before approving.

Placement. Artwork placement may vary up to 0.5 inches in any direction.

Materials. Embroidery appearance varies with fabric. A design approved on one garment or fabric may look different on another. Approval covers the garment, color and fabric shown on this proof.

Spoilage. Spoilage of up to 2% is considered acceptable and non-compensable.

Digitizing. Digitizing charges are non-refundable once work has begun. Digitized files remain the property of the shop unless otherwise agreed in writing.

Timing. Quoted turnaround begins on approval and does not include time spent awaiting approval.

Changes after approval. Changes requested after approval may incur additional charges and may affect the delivery date.

### Consent text (shown at the approve action, not editable below a minimum)

By typing my name and selecting Approve, I confirm that I have reviewed this proof, that I intend to approve it electronically, and that I agree to the terms above. I understand this approval applies to the specific design version, colorway, garment, color, size, placement and fabric shown. I consent to receiving this record electronically and understand I may request a paper copy at any time by contacting the shop. A copy of this approval and its record will be emailed to me.

### The honesty note (fixed, not editable)

What this proof shows and what it doesn't. This image is generated from the actual embroidery file that will run on the machine, so the stitch count, dimensions and thread sequence are exact. But embroidery is thread on fabric: it stretches, compresses and catches light in ways a screen cannot reproduce, and fine detail behaves differently on different materials. Treat this as an accurate plan, not a photograph of the finished piece.

## Appendix B · Sources

Research underpinning the market, workflow, technical and pricing claims in this document.

Workflow and practice — Impressions: problematic artwork · Impressions: digitizing disasters, lettering and density · Impressions: push/pull distortion · Impressions: backing selection · Impressions: cap fundamentals · ASI: cap digitizing · The Embroidery Coach: approval form · Printavo: use proofs, save money · Embroidery Legacy: small fonts · ColDesi: digitizing for small text · SEDDI: decorator mockup time · Adpro Imprints terms · Production Embroidery terms

Competitive and pricing — GraphicsFlow pricing · Printavo pricing · ShopWorks ProofStuff · DecoNetwork artwork approvals · YoPrint pricing · Ziflow pricing · Filestage pricing · Approval Studio pricing · PageProof pricing · Wilcom Workspace · Dynamic Mockups pricing

Technical — pystitch · Wilcom: thread charts and color index · DST format reference · colour-science ΔE · libvips displacement mapping · Hackaday: Pantone licensing · Twilio A2P 10DLC costs and throughput · Telnyx 10DLC fees · Docusign: ESIGN and UETA · Dropbox Sign: audit trails in court · HTML-to-PDF benchmark 2026 · rembg
