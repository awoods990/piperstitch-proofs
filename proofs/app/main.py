"""PiperStitch Proofs -- the service. Three auth realms with separate
dependencies (PRD "API surface"): the shop (session cookie), the public
customer surface (a token in the URL), and one unauthenticated
content-addressed verification route.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import auth, billing, colorways, config, core_client, db as database, events, garments, ingest, intake, pdfgen, proofs, reminders, sms, storage, stitch, texts, tokens
from .db import Account, AccountUser, ApprovalRecord, ChangeRequest, Contact, File, InboundEmail, IntakeAnswer, Message, Proof, ProofVersion, TermsVersion, TriageFinding, TriageReport, User

log = logging.getLogger("proofs")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    missing = config.require_for_serving()
    if missing:
        log.warning("Proofs is running with missing configuration: %s", ", ".join(missing))
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


async def _scheduler():
    await asyncio.sleep(15)
    while True:
        try:
            with database.SessionLocal() as db:
                n = proofs.expire_due(db) + intake.expire_intakes(db)
                r = reminders.run_due(db)
                db.commit()
                if n or r["sent"]:
                    log.info("Expired %d; reminders sent %d, suppressed %d", n, r["sent"], r["suppressed"])
        except Exception as e:  # noqa: BLE001
            log.exception("scheduler: %s", e)
        await asyncio.sleep(600)


app = FastAPI(title="PiperStitch Proofs", lifespan=_lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET or "dev-only-insecure-secret", same_site="lax", https_only=config.SESSION_COOKIE_SECURE)
_here = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")
templates = Jinja2Templates(directory=str(_here / "templates"))
templates.env.globals.update({"HONESTY_NOTE": texts.HONESTY_NOTE, "FABRIC_NAMES": texts.FABRIC_NAMES, "GARMENT_TEMPLATES": garments.TEMPLATES,
                              "GARMENT_COLORS": garments.GARMENT_COLORS, "TEMPLATE_BY_ID": garments.TEMPLATE_BY_ID, "PROOFS_PRICE_CENTS": config.PROOFS_PRICE_CENTS, "SMS_CONFIGURED": sms.configured()})


def _fmt_dt(value: str) -> str:
    if not value:
        return "—"
    return value.replace("T", " ").replace("Z", " UTC")


def _inches(mm: float) -> str:
    return f"{mm / 25.4:.2f}"


def _duration(seconds: float) -> str:
    total = int(round(seconds or 0))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    if h:
        return f"{h} h {m} min"
    return f"{m} min {s} s" if m else f"{s} s"


templates.env.filters.update({"dt": _fmt_dt, "inches": _inches, "duration": _duration})


def get_db():
    yield from database.session()


# --- shop realm ---------------------------------------------------------------------------

def current_member(request: Request, db: Session = Depends(get_db)) -> Optional[AccountUser]:
    return auth.session_member(db, request.session.get("token"))


def require_member(request: Request, db: Session = Depends(get_db)) -> AccountUser:
    m = auth.session_member(db, request.session.get("token"))
    if m is None:
        raise HTTPException(status_code=303, headers={"Location": "/signin"})
    return m


def require_can(action: str):
    def dep(m: AccountUser = Depends(require_member)) -> AccountUser:
        if not auth.can(m.role, action):
            raise HTTPException(status_code=403, detail=f"Your role ({m.role}) can't {action.replace('_', ' ')}.")
        return m
    return dep


def _client(request: Request) -> tuple[str, str]:
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    return ip, request.headers.get("user-agent", "")[:500]


def _base_url(request: Request) -> str:
    return config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _load_proof(db: Session, m: AccountUser, proof_id: str) -> Proof:
    p = db.get(Proof, proof_id)
    if p is None or p.account_id != m.account_id:
        raise HTTPException(status_code=404, detail="No such proof.")
    return p


@app.get("/health")
def health():
    return {"ok": True, "service": "piperstitch-proofs"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, m: Optional[AccountUser] = Depends(current_member)):
    if m is None:
        return RedirectResponse("/signin", status_code=303)
    return RedirectResponse("/proofs", status_code=303)


@app.get("/signin", response_class=HTMLResponse)
def signin_form(request: Request):
    return templates.TemplateResponse(request, "signin.html", {"step": "email", "error": request.session.pop("flash_error", None)})


@app.post("/signin", response_class=HTMLResponse)
def signin_request(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    try:
        path = auth.request_code(db, email)
        db.commit()
    except auth.AuthError as e:
        return templates.TemplateResponse(request, "signin.html", {"step": "email", "error": str(e), "email": email})
    return templates.TemplateResponse(request, "signin.html", {"step": "code", "email": email.strip().lower(), "path": path})


@app.post("/signin/verify", response_class=HTMLResponse)
def signin_verify(request: Request, email: str = Form(...), code: str = Form(...), db: Session = Depends(get_db)):
    try:
        token, member = auth.verify_code(db, email, code)
        db.commit()
    except auth.AuthError as e:
        return templates.TemplateResponse(request, "signin.html", {"step": "code", "email": email, "error": str(e)})
    request.session["token"] = token
    return RedirectResponse("/proofs", status_code=303)


@app.post("/signout")
def signout(request: Request, db: Session = Depends(get_db)):
    auth.sign_out(db, request.session.get("token"))
    db.commit()
    request.session.clear()
    return RedirectResponse("/signin", status_code=303)


BOARD_COLUMNS = [
    ("Waiting on you", ("draft", "digitizing", "ready_to_send", "changes_requested", "expired", "declined", "approved", "approved_with_notes", "art_received", "intake_expired", "internal_review")),
    ("Waiting on customer", ("sent", "viewed", "awaiting_art")),
    ("In production", ("released",)),
    ("Done", ("completed", "void")),
]


@app.get("/proofs", response_class=HTMLResponse)
def board(request: Request, m: AccountUser = Depends(require_member), db: Session = Depends(get_db)):
    rows = list(db.execute(select(Proof).where(Proof.account_id == m.account_id, Proof.archived_at.is_(None)).order_by(Proof.updated_at.desc())).scalars())
    columns = []
    for label, states in BOARD_COLUMNS:
        columns.append((label, [p for p in rows if p.status in states]))
    ent = proofs.entitlements(db, m.account)
    bounced = proofs.bounced_proofs(db, m.account_id)
    inbound = ingest.queue(db, m.account_id)
    db.commit()
    return templates.TemplateResponse(request, "board.html", {"m": m, "columns": columns, "ent": ent, "bounced": bounced, "inbound": inbound,
                                                               "art_address": f"art@{m.account.slug}.piperstitch.com" if m.account.slug else "",
                                                               "flash": request.session.pop("flash", None), "error": request.session.pop("flash_error", None)})


@app.get("/proofs/new", response_class=HTMLResponse)
def new_proof_form(request: Request, m: AccountUser = Depends(require_can("create")), db: Session = Depends(get_db)):
    projects = []
    project_error = ""
    if m.core_session_token and core_client.license_admin.configured:
        try:
            projects = core_client.license_admin.list_projects(m.core_session_token)
        except core_client.CoreError as e:
            project_error = str(e)
    contacts = list(db.execute(select(Contact).where(Contact.account_id == m.account_id, Contact.archived_at.is_(None)).order_by(Contact.display_name)).scalars())
    return templates.TemplateResponse(request, "proof_new.html", {"m": m, "projects": projects, "project_error": project_error, "contacts": contacts})


@app.post("/proofs/new")
def new_proof(request: Request, m: AccountUser = Depends(require_can("create")), db: Session = Depends(get_db),
              title: str = Form(""), contact_name: str = Form(""), contact_email: str = Form(""), contact_company: str = Form(""),
              contact_phone: str = Form(""), core_project_id: str = Form(""), core_project_name: str = Form(""), due_date: str = Form("")):
    if not contact_email.strip() and not contact_name.strip():
        request.session["flash_error"] = "Who is this proof for? Add a name or an email."
        return RedirectResponse("/proofs/new", status_code=303)
    contact = proofs.find_or_create_contact(db, m.account, display_name=contact_name, email=contact_email, company_name=contact_company, phone=contact_phone)
    p = proofs.create_proof(db, m.account, m.user, contact=contact, title=title or core_project_name or "Untitled",
                            core_project_id=core_project_id or None, core_project_name=core_project_name, due_date=due_date or None)
    db.commit()
    return RedirectResponse(f"/proofs/{p.id}", status_code=303)


def _proof_context(db: Session, m: AccountUser, p: Proof) -> dict:
    versions = list(p.versions)
    current = db.get(ProofVersion, p.current_version_id) if p.current_version_id else None
    changes = list(db.execute(select(ChangeRequest).where(ChangeRequest.proof_version_id == current.id).order_by(ChangeRequest.sequence)).scalars()) if current else []
    messages = list(db.execute(select(Message).where(Message.proof_id == p.id).order_by(Message.created_at)).scalars())
    approval = db.execute(select(ApprovalRecord).where(ApprovalRecord.proof_version_id == current.id)).scalar_one_or_none() if current else None
    chain = events.chain(db, p.id)
    chain_ok, chain_msg = events.verify_chain(db, p.id)
    reports = list(db.execute(select(TriageReport).where(TriageReport.proof_id == p.id).order_by(TriageReport.generated_at)).scalars())
    answers = list(db.execute(select(IntakeAnswer).where(IntakeAnswer.proof_id == p.id).order_by(IntakeAnswer.submitted_at)).scalars())
    return {"m": m, "p": p, "versions": versions, "current": current, "changes": changes, "messages": messages, "approval": approval,
            "chain": chain, "chain_ok": chain_ok, "chain_msg": chain_msg, "can": lambda a: auth.can(m.role, a), "ent": proofs.entitlements(db, m.account),
            "reports": reports, "blockers": intake.open_blockers(db, p), "answers": answers, "questions": dict((q[0], q[1]) for q in intake.QUESTIONS)}


@app.get("/proofs/{proof_id}", response_class=HTMLResponse)
def proof_detail(request: Request, proof_id: str, m: AccountUser = Depends(require_member), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    ctx = _proof_context(db, m, p)
    ctx.update({"flash": request.session.pop("flash", None), "error": request.session.pop("flash_error", None)})
    db.commit()
    return templates.TemplateResponse(request, "proof_detail.html", ctx)


@app.get("/proofs/{proof_id}/compose", response_class=HTMLResponse)
def compose_form(request: Request, proof_id: str, m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    project = None
    project_error = ""
    if p.core_project_id and m.core_session_token and core_client.license_admin.configured:
        try:
            project = core_client.license_admin.get_project(m.core_session_token, p.core_project_id)
        except core_client.CoreError as e:
            project_error = str(e)
    previous = db.get(ProofVersion, p.current_version_id) if p.current_version_id else None
    return templates.TemplateResponse(request, "compose.html", {"m": m, "p": p, "project": project, "project_error": project_error, "previous": previous,
                                                                 "error": request.session.pop("flash_error", None)})


@app.post("/proofs/{proof_id}/compose")
async def compose(request: Request, proof_id: str, m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    form = await request.form()
    document: Optional[dict] = None
    upload = form.get("document_file")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            document = json.loads((await upload.read()).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request.session["flash_error"] = "That file isn't a PiperStitch project (.stitchpilot JSON)."
            return RedirectResponse(f"/proofs/{p.id}/compose", status_code=303)
    elif p.core_project_id and m.core_session_token and core_client.license_admin.configured:
        try:
            project = core_client.license_admin.get_project(m.core_session_token, p.core_project_id)
            document = project.get("document") if isinstance(project.get("document"), dict) else json.loads(project["document"])
        except (core_client.CoreError, KeyError, ValueError) as e:
            request.session["flash_error"] = f"Couldn't fetch the project from PiperStitch: {e}"
            return RedirectResponse(f"/proofs/{p.id}/compose", status_code=303)
    if document is None:
        request.session["flash_error"] = "Choose a saved project or upload a .stitchpilot file."
        return RedirectResponse(f"/proofs/{p.id}/compose", status_code=303)
    if intake.open_blockers(db, p):
        request.session["flash_error"] = "The Readiness Report has unresolved blockers. Resolve or override them on the proof page before composing."
        return RedirectResponse(f"/proofs/{p.id}", status_code=303)
    try:
        v = proofs.compose_version(
            db, p, m.user, document=document, garment_style_name=str(form.get("garment_style_name", "")), garment_color=str(form.get("garment_color", "")),
            placement_name=str(form.get("placement_name", "")), placement_notes=str(form.get("placement_notes", "")),
            quantity=int(form.get("quantity") or 0), size_breakdown=proofs._size_breakdown_from_form(str(form.get("size_breakdown", ""))),
            message_body=str(form.get("message_body", "")), price_line=str(form.get("price_line", "")), hoop_code=str(form.get("hoop_code", "")),
            garment_template_id=str(form.get("garment_template_id", "")), garment_zone=str(form.get("garment_zone", "")),
            placement_down_mm=_mm(form.get("placement_down"), form.get("placement_units")), placement_across_mm=_mm(form.get("placement_across"), form.get("placement_units")))
        db.commit()
    except (proofs.TransitionError, core_client.CoreError) as e:
        db.rollback()
        request.session["flash_error"] = str(e)
        return RedirectResponse(f"/proofs/{p.id}/compose", status_code=303)
    request.session["flash"] = f"Version {v.version_number} composed: {v.stitch_count:,} stitches, {v.width_mm:.1f} × {v.height_mm:.1f} mm."
    # The fabric comes from the Core project; the garment from this form. Say so when they disagree.
    template = garments.TEMPLATE_BY_ID.get(v.garment_template_id)
    fabric_is_cap = v.fabric_code in ("structuredCap", "unstructuredCap", "beanie")
    if template and (template.category == "cap") != fabric_is_cap:
        request.session["flash_error"] = (f"Heads up: the PiperStitch project is set up for “{texts.FABRIC_NAMES.get(v.fabric_code, v.fabric_code)}” but this proof is on a {template.name.lower()}. "
                                          "The customer will see both. If that's wrong, change the fabric in PiperStitch and compose again.")
    return RedirectResponse(f"/proofs/{p.id}", status_code=303)


def _mm(value, units) -> float:
    try:
        v = float(str(value or "0").strip() or 0)
    except ValueError:
        return 0.0
    return v * 25.4 if (units or "in") == "in" else v


def _action(request: Request, db: Session, fn, ok_message: str, back: str):
    try:
        result = fn()
        db.commit()
        request.session["flash"] = ok_message.format(result=result)
    except (proofs.TransitionError, auth.AuthError, core_client.CoreError) as e:
        db.rollback()
        request.session["flash_error"] = str(e)
    return RedirectResponse(back, status_code=303)


@app.post("/proofs/{proof_id}/versions/{version_id}/send")
def send(request: Request, proof_id: str, version_id: str, m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, version_id)
    if v is None or v.proof_id != p.id:
        raise HTTPException(404)
    return _action(request, db, lambda: proofs.send_version(db, v, m.user, base_url=_base_url(request)), "Sent. Customer link: {result}", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/versions/{version_id}/resend")
def resend(request: Request, proof_id: str, version_id: str, m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, version_id)
    if v is None or v.proof_id != p.id:
        raise HTTPException(404)
    return _action(request, db, lambda: proofs.resend(db, v, m.user, base_url=_base_url(request)), "New link sent: {result}", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/release")
def release(request: Request, proof_id: str, m: AccountUser = Depends(require_can("release")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: proofs.release(db, p, m.user), "Released to production. The run ticket is ready.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/complete")
def complete(request: Request, proof_id: str, m: AccountUser = Depends(require_can("complete")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: proofs.complete(db, p, m.user), "Marked sewn.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/void")
def void(request: Request, proof_id: str, reason: str = Form(""), m: AccountUser = Depends(require_can("void")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: proofs.void(db, p, m.user, reason=reason), "Proof voided.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/reply")
def reply(request: Request, proof_id: str, body: str = Form(""), m: AccountUser = Depends(require_can("respond")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: proofs.answer_question(db, p, m.user, body=body, base_url=_base_url(request)), "Reply sent.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/approve-on-behalf")
def approve_on_behalf(request: Request, proof_id: str, signer_name: str = Form(""), channel: str = Form("phone"), evidence: str = Form(""),
                      notes: str = Form(""), m: AccountUser = Depends(require_can("on_behalf")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, p.current_version_id) if p.current_version_id else None
    if v is None:
        raise HTTPException(404)
    contact = db.get(Contact, p.contact_id)
    ip, ua = _client(request)
    return _action(request, db, lambda: proofs.approve(db, v, signer_name=signer_name, signer_email=contact.email, notes=notes, token_hash="", ip=ip,
                                                       user_agent=ua, method="on_behalf", on_behalf_channel=channel, on_behalf_evidence=evidence,
                                                       recorded_by=m.user, base_url=_base_url(request)),
                   "Approval recorded on the customer's behalf; certificate written.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/versions/{version_id}/colorways")
async def add_colorway(request: Request, proof_id: str, version_id: str, m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, version_id)
    if v is None or v.proof_id != p.id:
        raise HTTPException(404)
    form = await request.form()
    stops = []
    for s in v.thread_stops:
        stops.append({"hex": str(form.get(f"hex_{s.stop_number}", s.hex)), "name": str(form.get(f"name_{s.stop_number}", "")), "code": str(form.get(f"code_{s.stop_number}", ""))})
    return _action(request, db, lambda: colorways.add_colorway(db, v, m.user, name=str(form.get("name", "")), stop_colors=stops), "Colorway added: {result.name}", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/review/request")
def review_request(request: Request, proof_id: str, m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, p.current_version_id) if p.current_version_id else None
    if v is None:
        raise HTTPException(404)
    return _action(request, db, lambda: proofs.request_internal_review(db, v, m.user), "Sent for internal review.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/review/pass")
def review_pass(request: Request, proof_id: str, m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, p.current_version_id)
    return _action(request, db, lambda: proofs.pass_internal_review(db, v, m.user), "Passed review; ready to send.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/review/fail")
def review_fail(request: Request, proof_id: str, note: str = Form(""), m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, p.current_version_id)
    return _action(request, db, lambda: proofs.fail_internal_review(db, v, m.user, note=note), "Sent back with your note.", f"/proofs/{p.id}")


@app.post("/webhooks/postmark/inbound")
async def postmark_inbound(request: Request, db: Session = Depends(get_db)):
    if config.WEBHOOK_TOKEN and request.query_params.get("token") != config.WEBHOOK_TOKEN:
        raise HTTPException(401)
    body = await request.json()
    row = ingest.receive(db, body)
    db.commit()
    return {"status": row.status, "reason": row.reason, "proof_id": row.proof_id}


@app.post("/inbound/{email_id}/accept")
def inbound_accept(request: Request, email_id: str, m: AccountUser = Depends(require_can("create")), db: Session = Depends(get_db)):
    row = db.get(InboundEmail, email_id)
    if row is None or row.account_id != m.account_id:
        raise HTTPException(404)
    return _action(request, db, lambda: ingest.accept(db, row, m.user), "Accepted: proof created.", "/proofs")


@app.post("/inbound/{email_id}/reject")
def inbound_reject(request: Request, email_id: str, m: AccountUser = Depends(require_can("create")), db: Session = Depends(get_db)):
    row = db.get(InboundEmail, email_id)
    if row is None or row.account_id != m.account_id:
        raise HTTPException(404)
    return _action(request, db, lambda: ingest.reject(db, row, m.user), "Rejected.", "/proofs")


@app.post("/billing/checkout")
def billing_checkout(request: Request, m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db)):
    try:
        url = billing.checkout_url(db, m.account, email=m.user.email, base_url=_base_url(request))
        db.commit()
    except (proofs.TransitionError, Exception) as e:  # noqa: BLE001  (Stripe errors are shown, not swallowed)
        db.rollback()
        request.session["flash_error"] = str(e)
        return RedirectResponse("/settings", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.post("/billing/portal")
def billing_portal(request: Request, m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db)):
    try:
        url = billing.portal_url(db, m.account, base_url=_base_url(request))
    except (proofs.TransitionError, Exception) as e:  # noqa: BLE001
        request.session["flash_error"] = str(e)
        return RedirectResponse("/settings", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    try:
        result = billing.handle_webhook(db, payload, request.headers.get("stripe-signature", ""))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.warning("stripe webhook rejected: %s", e)
        raise HTTPException(status_code=400, detail="bad webhook")
    return {"result": result}


@app.post("/settings/art-address")
def art_address(request: Request, m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db)):
    return _action(request, db, lambda: ingest.make_slug(db, m.account), "Your art address is art@{result}.piperstitch.com", "/settings")


@app.post("/webhooks/twilio/inbound")
async def twilio_inbound(request: Request, db: Session = Depends(get_db)):
    """Twilio's inbound SMS/MMS webhook (set on the number or messaging service)."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = str(request.url)
    if config.PUBLIC_BASE_URL:
        url = config.PUBLIC_BASE_URL + request.url.path + (("?" + request.url.query) if request.url.query else "")
    if not sms.verify_signature(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403, detail="bad signature")
    reply = sms.inbound(db, form)
    db.commit()
    return Response(content=sms.twiml(reply), media_type="application/xml")


@app.post("/settings/sms")
def settings_sms(request: Request, enabled: str = Form("no"), m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db)):
    ent = proofs.entitlements(db, m.account)
    ent.sms_enabled = enabled == "yes"
    db.commit()
    request.session["flash"] = "Texting is on." if ent.sms_enabled else "Texting is off."
    return RedirectResponse("/settings", status_code=303)


@app.post("/webhooks/postmark/bounce")
async def postmark_bounce(request: Request, db: Session = Depends(get_db)):
    """Postmark's bounce webhook (configure with a secret token in the URL: /webhooks/postmark/bounce?token=...)."""
    if config.WEBHOOK_TOKEN and request.query_params.get("token") != config.WEBHOOK_TOKEN:
        raise HTTPException(401)
    body = await request.json()
    n = proofs.record_bounce(db, email=str(body.get("Email") or body.get("Recipient") or ""), reason=str(body.get("Type") or body.get("Description") or "bounce"))
    db.commit()
    return {"recorded": n}


@app.post("/proofs/{proof_id}/snooze")
def snooze(request: Request, proof_id: str, days: int = Form(3), m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: reminders.snooze(p, max(1, min(30, days))), "Reminders snoozed.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/intake/send")
def send_intake(request: Request, proof_id: str, message: str = Form(""), m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: intake.send_intake(db, p, m.user, base_url=_base_url(request), message=message), "Intake link sent: {result}", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/intake/upload")
async def shop_upload(request: Request, proof_id: str, m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    form = await request.form()
    uploads = []
    for u in form.getlist("files"):
        if getattr(u, "filename", ""):
            uploads.append((u.filename, await u.read()))
    width_mm = _mm(form.get("requested_width"), form.get("width_units")) or p.requested_width_mm
    is_cap = str(form.get("is_cap", "")) == "yes"
    return _action(request, db, lambda: intake.attach_files(db, p, m.user, uploads=uploads, requested_width_mm=width_mm, is_cap=is_cap),
                   "Artwork added and triaged.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/triage/override")
def triage_override(request: Request, proof_id: str, reason: str = Form(""), m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: intake.override_blockers(db, p, m.user, reason=reason), "{result} blocker(s) overridden; it's on the record.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/triage/{finding_id}/resolve")
def triage_resolve(request: Request, proof_id: str, finding_id: str, m: AccountUser = Depends(require_can("compose")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    fd = db.get(TriageFinding, finding_id)
    if fd is None or db.get(TriageReport, fd.triage_report_id).proof_id != p.id:
        raise HTTPException(404)
    return _action(request, db, lambda: intake.resolve_finding(db, fd, m.user), "Marked resolved.", f"/proofs/{p.id}")


@app.post("/proofs/{proof_id}/triage/share")
def triage_share(request: Request, proof_id: str, message: str = Form(""), m: AccountUser = Depends(require_can("send")), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    return _action(request, db, lambda: intake.share_report(db, p, m.user, message=message, base_url=_base_url(request)), "Report sent: {result}", f"/proofs/{p.id}")


@app.get("/proofs/{proof_id}/files/{file_id}")
def shop_file(proof_id: str, file_id: str, preview: int = 0, m: AccountUser = Depends(require_member), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    f = db.get(File, file_id)
    if f is None or f.proof_id != p.id:
        raise HTTPException(404)
    return _serve_file(db, f, preview=bool(preview))


def _serve_file(db: Session, f: File, *, preview: bool) -> Response:
    if preview:
        r = db.execute(select(TriageReport).where(TriageReport.file_id == f.id)).scalars().first()
        if r and r.preview_storage_key and storage.exists(r.preview_storage_key):
            return Response(content=storage.get(r.preview_storage_key), media_type="image/png")
        raise HTTPException(404)
    return Response(content=storage.get(f.storage_key), media_type=f.mime_type or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{f.original_filename}"'})


@app.get("/proofs/{proof_id}/versions/{version_id}/{artifact}")
def shop_artifact(proof_id: str, version_id: str, artifact: str, m: AccountUser = Depends(require_member), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, version_id)
    if v is None or v.proof_id != p.id:
        raise HTTPException(404)
    return _serve_artifact(p, v, artifact, gate=m.account.release_gate_policy)


def _serve_artifact(p: Proof, v: ProofVersion, artifact: str, *, gate: str = "off") -> Response:
    names = {"proof.pdf": ("proof.pdf", "application/pdf"), "render.png": ("render.png", "image/png"), "hero.png": ("hero.png", "image/png"),
             "social.png": ("social.png", "image/png"), "mockup.png": ("mockup.png", "image/png"), "diagram.png": ("diagram.png", "image/png")}
    for fmt in core_client.MACHINE_FORMATS:
        names[f"design.{fmt}"] = (f"design.{fmt}", "application/octet-stream")
    for o in range(2, colorways.MAX_COLORWAYS + 1):
        names[f"cw{o}-render.png"] = (f"cw{o}-render.png", "image/png")
        names[f"cw{o}-mockup.png"] = (f"cw{o}-mockup.png", "image/png")
    if artifact not in names:
        raise HTTPException(404)
    if artifact.startswith("design.") and gate == "hard" and p.status not in ("released", "completed"):
        raise HTTPException(status_code=423, detail="Machine files are withheld until the proof is approved and released (release gate: hard).")
    key = proofs._artifact_key(p, v.version_number, names[artifact][0])
    if not storage.exists(key):
        raise HTTPException(404)
    data = storage.get(key)
    headers = {"Content-Disposition": f'inline; filename="{p.reference}-v{v.version_number}-{names[artifact][0]}"'}
    if artifact.startswith("design.") and gate == "soft" and p.status not in ("released", "completed"):
        headers["X-PiperStitch-Warning"] = "unapproved"
    return Response(content=data, media_type=names[artifact][1], headers=headers)


@app.get("/proofs/{proof_id}/run-ticket.pdf")
def run_ticket(proof_id: str, m: AccountUser = Depends(require_member), db: Session = Depends(get_db)):
    p = _load_proof(db, m, proof_id)
    v = db.get(ProofVersion, p.current_version_id) if p.current_version_id else None
    if v is None:
        raise HTTPException(404, "Nothing composed yet.")
    approval = db.execute(select(ApprovalRecord).where(ApprovalRecord.proof_version_id == v.id)).scalar_one_or_none()
    render = storage.get(proofs._artifact_key(p, v.version_number, "render.png"))
    diagram_key = proofs._artifact_key(p, v.version_number, "diagram.png")
    pdf = pdfgen.run_ticket_pdf(diagram_png=storage.get(diagram_key) if storage.exists(diagram_key) else None,
        shop_name=m.account.shop_name or "PiperStitch Proofs", reference=p.reference, title=p.title, version_number=v.version_number, render_png=render,
        stops=[proofs.stop_dict(s) for s in v.thread_stops], width_mm=v.width_mm, height_mm=v.height_mm, stitch_count=v.stitch_count,
        color_change_count=v.color_change_count, trim_count=v.trim_count, hoop=v.hoop_code, fabric_name=texts.FABRIC_NAMES.get(v.fabric_code, v.fabric_code),
        stabilizer=v.stabilizer_advice, garment=v.garment_style_name, garment_color=v.garment_color, placement=v.placement_name, placement_notes=v.placement_notes,
        quantity=v.quantity, size_breakdown=v.size_breakdown, estimated_run=_duration(v.estimated_run_seconds),
        approved_at=approval.approved_at if approval else "", approved_by=approval.signer_name_typed if approval else "",
        machine_files=v.artifact_hashes.get("machine_files", {}), cleared=p.status in ("released", "completed"))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{p.reference}-run-ticket.pdf"'})


@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db)):
    terms = proofs.current_terms(db, m.account)
    members = list(db.execute(select(AccountUser).where(AccountUser.account_id == m.account_id, AccountUser.disabled_at.is_(None))).scalars())
    ent = proofs.entitlements(db, m.account)
    db.commit()
    return templates.TemplateResponse(request, "settings.html", {"m": m, "terms": terms, "members": members, "ent": ent,
                                                                  "flash": request.session.pop("flash", None), "error": request.session.pop("flash_error", None)})


@app.post("/settings")
def settings_save(request: Request, m: AccountUser = Depends(require_can("settings")), db: Session = Depends(get_db),
                  shop_name: str = Form(""), reply_to_email: str = Form(""), phone: str = Form(""), release_gate_policy: str = Form("soft"),
                  default_response_window_days: int = Form(7), terms_body: str = Form(""), consent_text: str = Form(""),
                  quiet_hours_start: int = Form(20), quiet_hours_end: int = Form(8), reminders_enabled: str = Form("yes")):
    a = m.account
    a.shop_name, a.reply_to_email, a.phone = shop_name.strip(), reply_to_email.strip(), phone.strip()
    a.quiet_hours_start, a.quiet_hours_end = max(0, min(23, quiet_hours_start)), max(0, min(23, quiet_hours_end))
    a.reminders_enabled = reminders_enabled == "yes"
    a.release_gate_policy = release_gate_policy if release_gate_policy in ("hard", "soft", "off") else "soft"
    a.default_response_window_days = max(1, min(60, default_response_window_days))
    current = proofs.current_terms(db, a)
    if terms_body.strip() != current.body.strip() or consent_text.strip() != current.consent_text.strip():
        return _action(request, db, lambda: proofs.set_terms(db, a, body=terms_body.strip(), consent_text=consent_text.strip()), "Settings saved; terms are now {result.label}.", "/settings")
    db.commit()
    request.session["flash"] = "Settings saved."
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/invite")
def invite(request: Request, email: str = Form(...), role: str = Form("sales"), m: AccountUser = Depends(require_can("invite")), db: Session = Depends(get_db)):
    return _action(request, db, lambda: auth.invite(db, m.account, email=email, role=role), "Invited.", "/settings")


# --- public token realm ---------------------------------------------------------------------

def _public_version(db: Session, plaintext: str) -> tuple[tokens.TokenState, Optional[ProofVersion], Optional[Proof]]:
    state = tokens.resolve(db, plaintext, purpose="proof")
    if state.token is None:
        return state, None, None
    v = db.get(ProofVersion, state.token.proof_version_id) if state.token.proof_version_id else None
    p = db.get(Proof, state.token.proof_id)
    return state, v, p


def _public_context(db: Session, p: Proof, v: ProofVersion, plaintext: str) -> dict:
    account = db.get(Account, p.account_id)
    contact = db.get(Contact, p.contact_id)
    terms = db.get(TermsVersion, v.terms_version_id) if v.terms_version_id else None
    approval = db.execute(select(ApprovalRecord).where(ApprovalRecord.proof_version_id == v.id)).scalar_one_or_none()
    changes = list(db.execute(select(ChangeRequest).where(ChangeRequest.proof_version_id == v.id).order_by(ChangeRequest.sequence)).scalars())
    messages = list(db.execute(select(Message).where(Message.proof_id == p.id).order_by(Message.created_at)).scalars())
    return {"p": p, "v": v, "account": account, "contact": contact, "terms": terms, "approval": approval, "changes": changes, "messages": messages,
            "token": plaintext, "version_count": len(p.versions), "gate": account.release_gate_policy}


@app.get("/p/{plaintext}", response_class=HTMLResponse)
def public_proof(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if p is not None and p.status == "void":
        db.commit()
        return templates.TemplateResponse(request, "public_unavailable.html", {"reason": "void", "p": p})
    if not state.ok or v is None:
        ctx = {"reason": state.reason, "p": p, "token": plaintext if state.token else ""}
        db.commit()
        return templates.TemplateResponse(request, "public_unavailable.html", ctx, status_code=200)
    ip, ua = _client(request)
    proofs.record_view(db, v, token_hash=state.token.token_hash, ip=ip, user_agent=ua)
    tokens.extend_on_activity(state.token)
    ctx = _public_context(db, p, v, plaintext)
    ctx.update({"flash": request.session.pop("flash", None), "error": request.session.pop("flash_error", None)})
    db.commit()
    return templates.TemplateResponse(request, "public_proof.html", ctx)


@app.get("/p/{plaintext}/{artifact}")
def public_artifact(plaintext: str, artifact: str, db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    if artifact.startswith("design."):
        raise HTTPException(404)   # machine files are never on the customer link
    db.commit()
    return _serve_artifact(p, v, artifact)


@app.post("/p/{plaintext}/approve")
def public_approve(request: Request, plaintext: str, signer_name: str = Form(""), signer_email: str = Form(""), notes: str = Form(""),
                   consent: str = Form(""), local_offset: str = Form(""), colorway: int = Form(1), db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    if consent != "yes":
        request.session["flash_error"] = "Please tick the consent box to approve."
        return RedirectResponse(f"/p/{plaintext}#approve", status_code=303)
    ip, ua = _client(request)
    try:
        record = proofs.approve(db, v, signer_name=signer_name, signer_email=signer_email or db.get(Contact, p.contact_id).email, notes=notes,
                                token_hash=state.token.token_hash, ip=ip, user_agent=ua, local_offset=local_offset, base_url=_base_url(request), colorway_ordinal=colorway)
        db.commit()
    except proofs.TransitionError as e:
        db.rollback()
        request.session["flash_error"] = str(e)
        return RedirectResponse(f"/p/{plaintext}#approve", status_code=303)
    request.session["flash"] = f"Approved. A copy of your certificate was sent to {record.signer_email or 'you'}."
    return RedirectResponse(f"/p/{plaintext}", status_code=303)


@app.post("/p/{plaintext}/changes")
async def public_changes(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    form = await request.form()
    items = []
    for chip in form.getlist("chip"):
        items.append({"kind": "chip", "chip_code": str(chip), "body": ""})
    if str(form.get("body", "")).strip():
        items.append({"kind": "freetext", "body": str(form.get("body"))})
    pins = str(form.get("pins", "")).strip()
    if pins:
        try:
            for pin in json.loads(pins):
                items.append({"kind": "pin", "body": pin.get("note", ""), "pin_x": float(pin["x"]), "pin_y": float(pin["y"])})
        except (ValueError, KeyError, TypeError):
            pass
    ip, ua = _client(request)
    try:
        proofs.request_changes(db, v, items=items, token_hash=state.token.token_hash, ip=ip, user_agent=ua)
        db.commit()
        request.session["flash"] = "Thanks -- the shop has your change requests."
    except proofs.TransitionError as e:
        db.rollback()
        request.session["flash_error"] = str(e)
    return RedirectResponse(f"/p/{plaintext}", status_code=303)


@app.post("/p/{plaintext}/question")
def public_question(request: Request, plaintext: str, body: str = Form(""), db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    ip, ua = _client(request)
    try:
        proofs.ask_question(db, v, body=body, token_hash=state.token.token_hash, ip=ip, user_agent=ua)
        db.commit()
        request.session["flash"] = "Sent. The shop will reply by email."
    except proofs.TransitionError as e:
        db.rollback()
        request.session["flash_error"] = str(e)
    return RedirectResponse(f"/p/{plaintext}#questions", status_code=303)


@app.post("/p/{plaintext}/decline")
def public_decline(request: Request, plaintext: str, reason: str = Form(""), db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    ip, ua = _client(request)
    try:
        proofs.decline(db, v, reason=reason, token_hash=state.token.token_hash, ip=ip, user_agent=ua)
        db.commit()
    except proofs.TransitionError as e:
        db.rollback()
        request.session["flash_error"] = str(e)
    return RedirectResponse(f"/p/{plaintext}", status_code=303)


@app.get("/p/{plaintext}/compare/{n}/difference.png")
def public_difference(plaintext: str, n: int, db: Session = Depends(get_db)):
    """Version n against the current one, as a difference image."""
    state, v, p = _public_version(db, plaintext)
    if not state.ok or v is None:
        raise HTTPException(404)
    old = next((x for x in p.versions if x.version_number == n), None)
    if old is None or old.id == v.id:
        raise HTTPException(404)
    a = storage.get(proofs._artifact_key(p, old.version_number, "render.png"))
    b = storage.get(proofs._artifact_key(p, v.version_number, "render.png"))
    return Response(content=stitch.difference_png(a, b), media_type="image/png")


@app.get("/p/{plaintext}/versions/{n}", response_class=HTMLResponse)
def public_version(request: Request, plaintext: str, n: int, db: Session = Depends(get_db)):
    """Read-only view of an earlier version through the current link."""
    state, v, p = _public_version(db, plaintext)
    if state.token is None or p is None:
        raise HTTPException(404)
    old = next((x for x in p.versions if x.version_number == n), None)
    if old is None:
        raise HTTPException(404)
    ctx = _public_context(db, p, old, plaintext)
    ctx["read_only"] = True
    db.commit()
    return templates.TemplateResponse(request, "public_proof.html", ctx)


@app.get("/p/{plaintext}/versions/{n}/{artifact}")
def public_version_artifact(plaintext: str, n: int, artifact: str, db: Session = Depends(get_db)):
    state, v, p = _public_version(db, plaintext)
    if state.token is None or p is None or artifact.startswith("design."):
        raise HTTPException(404)
    old = next((x for x in p.versions if x.version_number == n), None)
    if old is None:
        raise HTTPException(404)
    return _serve_artifact(p, old, artifact)


# --- public intake and readiness report ----------------------------------------------------------

@app.get("/i/{plaintext}", response_class=HTMLResponse)
def intake_form(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="intake")
    p = db.get(Proof, state.token.proof_id) if state.token else None
    if not state.ok or p is None or p.status != "awaiting_art":
        reason = "done" if (p is not None and p.status not in ("awaiting_art", "void")) else ("void" if p is not None and p.status == "void" else state.reason)
        db.commit()
        return templates.TemplateResponse(request, "intake_unavailable.html", {"reason": reason, "p": p})
    account = db.get(Account, p.account_id)
    contact = db.get(Contact, p.contact_id)
    if not any(e.event_type == "intake_opened" for e in events.chain(db, p.id)):
        ip, ua = _client(request)
        events.append(db, proof_id=p.id, event_type="intake_opened", actor_type="contact", actor_id=contact.id, token_hash=state.token.token_hash, ip=ip, user_agent=ua)
    db.commit()
    return templates.TemplateResponse(request, "intake.html", {"p": p, "account": account, "contact": contact, "token": plaintext, "questions": intake.QUESTIONS,
                                                                "accepted": ", ".join(intake.ACCEPTED_EXTENSIONS), "error": request.session.pop("flash_error", None)})


@app.post("/i/{plaintext}")
async def intake_submit(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="intake")
    p = db.get(Proof, state.token.proof_id) if state.token else None
    if not state.ok or p is None or p.status != "awaiting_art":
        raise HTTPException(404)
    form = await request.form()
    uploads = []
    for u in form.getlist("files"):
        if getattr(u, "filename", ""):
            uploads.append((u.filename, await u.read()))
    answers = {q[0]: str(form.get(q[0], "")) for q in intake.QUESTIONS}
    ip, ua = _client(request)
    try:
        intake.receive_intake(db, p, token_hash=state.token.token_hash, uploads=uploads, answers=answers, ip=ip, user_agent=ua,
                              sms_consent=str(form.get("sms_consent", "")) == "yes", phone=str(form.get("phone", "")))
        db.commit()
    except proofs.TransitionError as e:
        db.rollback()
        request.session["flash_error"] = str(e)
        return RedirectResponse(f"/i/{plaintext}", status_code=303)
    account = db.get(Account, p.account_id)
    return templates.TemplateResponse(request, "intake_unavailable.html", {"reason": "thanks", "p": p, "account": account})


@app.get("/r/{plaintext}", response_class=HTMLResponse)
def public_report(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="triage_report")
    if state.token is None:
        raise HTTPException(404)
    p = db.get(Proof, state.token.proof_id)
    account = db.get(Account, p.account_id)
    reports = list(db.execute(select(TriageReport).where(TriageReport.proof_id == p.id).order_by(TriageReport.generated_at)).scalars())
    db.commit()
    return templates.TemplateResponse(request, "report.html", {"p": p, "account": account, "reports": reports, "token": plaintext, "public": True})


@app.get("/r/{plaintext}/files/{file_id}/preview.png")
def public_report_preview(plaintext: str, file_id: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="triage_report")
    if state.token is None:
        raise HTTPException(404)
    f = db.get(File, file_id)
    if f is None or f.proof_id != state.token.proof_id:
        raise HTTPException(404)
    return _serve_file(db, f, preview=True)


# --- certificates and verification -----------------------------------------------------------

@app.get("/c/{plaintext}", response_class=HTMLResponse)
def certificate_page(request: Request, plaintext: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="certificate")
    if state.token is None:
        raise HTTPException(404)
    record = db.get(ApprovalRecord, state.token.approval_record_id)
    v = db.get(ProofVersion, record.proof_version_id)
    p = db.get(Proof, v.proof_id)
    account = db.get(Account, p.account_id)
    verification = proofs.verify_certificate(db, record.certificate_sha256)
    db.commit()
    return templates.TemplateResponse(request, "certificate.html", {"record": record, "v": v, "p": p, "account": account, "token": plaintext, "verification": verification})


@app.get("/c/{plaintext}/certificate.pdf")
def certificate_pdf(plaintext: str, db: Session = Depends(get_db)):
    state = tokens.resolve(db, plaintext, purpose="certificate")
    if state.token is None:
        raise HTTPException(404)
    record = db.get(ApprovalRecord, state.token.approval_record_id)
    data = storage.get(record.certificate_storage_key)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="certificate-of-approval.pdf"'})


@app.get("/verify/{sha256_hex}")
def verify(sha256_hex: str, db: Session = Depends(get_db)):
    result = proofs.verify_certificate(db, sha256_hex)
    return JSONResponse(result, status_code=200 if result["result"] == "pass" else 409)


@app.get("/verify", response_class=HTMLResponse)
def verify_form(request: Request):
    return templates.TemplateResponse(request, "verify.html", {})
