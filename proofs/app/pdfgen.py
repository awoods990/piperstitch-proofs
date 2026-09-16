"""The proof PDF, the run ticket and the Certificate of Approval, with
reportlab (PRD v1.1 change 4): every spec, measurement and thread code is
real selectable text; the render is embedded as an image.
"""

from __future__ import annotations

import io
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import texts

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_styles["Heading1"], fontSize=18, leading=22, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=_styles["Heading2"], fontSize=12, leading=15, spaceBefore=8, spaceAfter=3)
BODY = ParagraphStyle("body", parent=_styles["BodyText"], fontSize=9.5, leading=12.5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10.5, textColor=colors.HexColor("#555555"))
MONO = ParagraphStyle("mono", parent=BODY, fontName="Courier", fontSize=7.5, leading=9.5)


def _p(text: str, style=BODY) -> Paragraph:
    return Paragraph(text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"), style)


def _inches(mm_value: float) -> str:
    return f"{mm_value / 25.4:.2f} in"


def _render_flowable(render_png: bytes, max_w: float, max_h: float) -> RLImage:
    reader = ImageReader(io.BytesIO(render_png))
    w, h = reader.getSize()
    ratio = min(max_w / w, max_h / h)
    return RLImage(io.BytesIO(render_png), width=w * ratio, height=h * ratio)


def _stop_table(stops: list[dict]) -> Table:
    rows = [["Stop", "", "Thread", "Code", "Stitches"]]
    for s in stops:
        rows.append([str(s["stop_number"]), "", f"{s.get('thread_brand', '')} {s['thread_name']}".strip(), s.get("thread_code", "") or "—", f"{s['stitch_count']:,}"])
    t = Table(rows, colWidths=[0.5 * inch, 0.3 * inch, 3.0 * inch, 1.2 * inch, 1.0 * inch])
    style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
             ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey), ("ALIGN", (4, 1), (4, -1), "RIGHT"),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]
    for i, s in enumerate(stops, start=1):
        style.append(("BACKGROUND", (1, i), (1, i), colors.HexColor(s["hex"])))
    t.setStyle(TableStyle(style))
    return t


def _header(shop_name: str, heading: str, logo_png: Optional[bytes]) -> list:
    """Shop logo (if any) beside the heading."""
    title = _p(heading, H1)
    if not logo_png:
        return [title]
    try:
        logo = _render_flowable(logo_png, 1.8 * inch, 0.6 * inch)
    except Exception:
        return [title]
    t = Table([[logo, title]], colWidths=[2.0 * inch, 5.0 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return [t]


def proof_pdf(*, shop_name: str, reference: str, title: str, version_number: int, version_count: int, composed_at: str,
              contact_name: str, render_png: bytes, width_mm: float, height_mm: float, stitch_count: int,
              color_change_count: int, trim_count: int, stops: list[dict], garment: str, garment_color: str,
              placement: str, placement_notes: str, quantity: int, size_breakdown: dict, fabric_name: str,
              stabilizer: str, terms_body: str, message: str, price_line: str = "", design_hash: str = "",
              supersedes: Optional[int] = None, mockup_png: Optional[bytes] = None, diagram_png: Optional[bytes] = None,
              logo_png: Optional[bytes] = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"{reference} {title} — proof v{version_number}", author=shop_name)
    story = []
    story.extend(_header(shop_name, f"{shop_name} — Embroidery proof", logo_png))
    stamp = f"{reference} · {title} · Version {version_number} of {version_count} · created {composed_at[:10]}"
    if supersedes:
        stamp += f" · supersedes v{supersedes}"
    story.append(_p(stamp, SMALL))
    story.append(_p(f"Prepared for {contact_name}", SMALL))
    story.append(Spacer(1, 8))
    if mockup_png:
        pair = Table([[_render_flowable(mockup_png, 3.4 * inch, 3.4 * inch), _render_flowable(render_png, 3.4 * inch, 3.4 * inch)]], colWidths=[3.5 * inch, 3.5 * inch])
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(pair)
        story.append(_p("Left: on the garment at relative size (a drawing, not a photograph). Right: the stitch render from the actual machine file at true scale. " + texts.HONESTY_NOTE, SMALL))
    else:
        story.append(_render_flowable(render_png, 7.0 * inch, 4.2 * inch))
        story.append(_p("Rendered from the actual machine file at true scale. " + texts.HONESTY_NOTE, SMALL))
    story.append(Spacer(1, 6))

    specs = [
        ["Finished size", f"{_inches(width_mm)} × {_inches(height_mm)}  ({width_mm:.2f} × {height_mm:.2f} mm)"],
        ["Stitches", f"{stitch_count:,}  ·  {color_change_count} colour change{'s' if color_change_count != 1 else ''}  ·  {trim_count} trims"],
        ["Garment", f"{garment or '—'}{(' · ' + garment_color) if garment_color else ''}"],
        ["Placement", f"{placement or '—'}{(' — ' + placement_notes) if placement_notes else ''}"],
        ["Quantity", f"{quantity or 0}" + (("  (" + ", ".join(f"{k} × {v}" for k, v in size_breakdown.items()) + ")") if size_breakdown else "")],
        ["Fabric", fabric_name],
        ["Backing", stabilizer],
    ]
    if price_line:
        specs.append(["Price", price_line])
    t = Table([[_p(k, SMALL), _p(v, BODY)] for k, v in specs], colWidths=[1.2 * inch, 5.8 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2)]))
    story.append(t)
    story.append(_p("Thread stops, in sewing order", H2))
    story.append(_stop_table(stops))
    if diagram_png:
        story.append(_p("Placement", H2))
        story.append(_render_flowable(diagram_png, 3.6 * inch, 3.6 * inch))
    if message:
        story.append(_p("Note from the shop", H2))
        story.append(_p(message))
    story.append(_p("Approval terms", H2))
    story.append(_p(terms_body, SMALL))
    if design_hash:
        story.append(Spacer(1, 6))
        story.append(_p(f"Design fingerprint {design_hash}", MONO))
    doc.build(story)
    return buf.getvalue()


NEEDLE_ADVICE = {   # keyed by Core's fabric codes; a suggestion, the stitcher's call
    "standard": "75/11 sharp", "stableWoven": "75/11 sharp", "knit": "75/11 ballpoint", "stretchKnit": "75/11 ballpoint",
    "terry": "75/11 ballpoint", "leatherOrVinyl": "80/12 leather point",
    "structuredCap": "80/12 sharp", "unstructuredCap": "80/12 sharp", "beanie": "75/11 ballpoint",
}


def run_ticket_pdf(*, shop_name: str, reference: str, title: str, version_number: int, render_png: bytes, stops: list[dict],
                   width_mm: float, height_mm: float, stitch_count: int, color_change_count: int, trim_count: int,
                   hoop: str, fabric_name: str, stabilizer: str, garment: str, garment_color: str, placement: str,
                   placement_notes: str, quantity: int, size_breakdown: dict, estimated_run: str, approved_at: str,
                   approved_by: str, machine_files: dict, cleared: bool, diagram_png: Optional[bytes] = None,
                   fabric_code: str = "", colorway_name: str = "", approval_notes: str = "", customer: str = "",
                   due_date: str = "", logo_png: Optional[bytes] = None) -> bytes:
    """The production sheet: one page the stitcher pins to the machine.
    Everything is the *approved* version -- the chosen colourway's threads,
    the approved size and placement -- so nothing has to be looked up."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            title=f"{reference} production sheet", author=shop_name)
    BIG = ParagraphStyle("big", parent=BODY, fontSize=11, leading=14)
    LABEL = ParagraphStyle("label", parent=SMALL, fontSize=7.5, textColor=colors.HexColor("#6b6f76"))
    story = _header(shop_name, f"Production sheet — {reference}", logo_png)
    story.append(_p(f"{title} · version {version_number}" + (f" · for {customer}" if customer else "") + (f" · needed by {due_date}" if due_date else ""), BODY))
    banner = ParagraphStyle("banner", parent=H2, fontSize=13, textColor=colors.white, backColor=colors.HexColor("#1b7a3d" if cleared else "#b00020"),
                            borderPadding=(4, 6, 4, 6), spaceBefore=6, spaceAfter=8)
    story.append(_p(("CLEARED TO SEW — approved " + approved_at[:10] + " by " + approved_by) if cleared else "NOT CLEARED — do not sew until the job is released", banner))
    if colorway_name:
        story.append(_p(f"Customer chose colourway: {colorway_name}", BIG))
    if approval_notes:
        story.append(_p(f"Customer's note at approval: {approval_notes}", BODY))
    story.append(Spacer(1, 4))

    # Pictures: the stitch render and the measured placement.
    if diagram_png:
        pair = Table([[_render_flowable(render_png, 3.3 * inch, 2.6 * inch), _render_flowable(diagram_png, 3.6 * inch, 2.6 * inch)]], colWidths=[3.5 * inch, 3.8 * inch])
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(pair)
    else:
        story.append(_render_flowable(render_png, 3.6 * inch, 2.6 * inch))
    story.append(Spacer(1, 6))

    # Set-up: the big, glanceable facts.
    def cell(label, value):
        return [_p(label, LABEL), _p(value, BIG)]
    file_line = ", ".join(k.upper() for k in machine_files) or "—"
    sizes = ", ".join(f"{k} × {v}" for k, v in size_breakdown.items()) if size_breakdown else ""
    setup = [
        [cell("Hoop / frame", hoop or "—"), cell("Design size", f"{_inches(width_mm)} × {_inches(height_mm)}  ({width_mm:.1f} × {height_mm:.1f} mm)")],
        [cell("Garment", f"{garment or '—'}{(' · ' + garment_color) if garment_color else ''}"), cell("Placement", f"{placement or '—'}" + (f" — {placement_notes}" if placement_notes else ""))],
        [cell("Fabric", fabric_name), cell("Backing", stabilizer or "—")],
        [cell("Needle (suggested)", NEEDLE_ADVICE.get(fabric_code, "75/11")), cell("Machine file", f"{file_line} — {reference} v{version_number}")],
        [cell("Quantity", f"{quantity or 0}" + (f"  ({sizes})" if sizes else "")), cell("Stitches / run", f"{stitch_count:,} · {color_change_count} colour changes · {trim_count} trims · ~{estimated_run} per piece")],
    ]
    t = Table(setup, colWidths=[3.65 * inch, 3.65 * inch])   # a list in a cell stacks label over value
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9d4cb")),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(t)

    story.append(_p("Thread stops, in sewing order" + (f" — {colorway_name}" if colorway_name else ""), H2))
    story.append(_stop_table(stops))
    story.append(_p("Load the threads in this order before you start; the machine stops at each change.", SMALL))

    story.append(_p("Checklist", H2))
    checks = ["Hoop and backing as above; fabric taut, not stretched", "Threads loaded in stop order; needle as suggested",
              "Placement measured on the first piece and checked against the diagram", "First piece sewn and compared with the render before the run",
              "Count by size: " + (sizes or f"{quantity or 0} pieces")]
    ct = Table([["", _p(c, BODY)] for c in checks], colWidths=[0.3 * inch, 7.0 * inch], rowHeights=[16] * len(checks))
    ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOX", (0, 0), (0, 0), 0.8, colors.black)] +
                           [("BOX", (0, i), (0, i), 0.8, colors.black) for i in range(len(checks))] +
                           [("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 0)]))
    story.append(ct)
    story.append(Spacer(1, 10))
    sign = Table([[_p("Set up by ______________________", BODY), _p("First piece checked by ______________________", BODY), _p("Date ______________", BODY)]], colWidths=[2.4 * inch, 3.2 * inch, 1.7 * inch])
    story.append(sign)
    story.append(_p("Files: " + ", ".join(f"{k.upper()} {v[:12]}…" for k, v in machine_files.items()), MONO))
    doc.build(story)
    return buf.getvalue()


def certificate_pdf(*, shop_name: str, reference: str, title: str, version_number: int, approved_at: str, method: str,
                    signer_name: str, signer_email: str, signer_ip: str, signer_user_agent: str, on_behalf: str,
                    conditions: dict, artifact_hashes: dict, consent_text: str, terms_body: str, events: list[dict],
                    event_chain_head: str, verify_url: str, certificate_id: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Certificate of Approval {reference}", author="PiperStitch Proofs")
    story = [_p("Certificate of Approval", H1), _p(f"{shop_name} · {reference} · {title} · version {version_number}", SMALL),
             _p(f"Certificate {certificate_id}", MONO), Spacer(1, 8)]
    who = f"Approved {approved_at} by {signer_name or '—'} ({signer_email or 'no email'})"
    if method == "on_behalf":
        who += f" — recorded by the shop on the customer's behalf ({on_behalf})"
    story.append(_p(who))
    story.append(_p(f"From IP {signer_ip or '—'} · {signer_user_agent or '—'}", SMALL))
    story.append(_p("What was approved", H2))
    rows = [[_p(k.replace("_", " "), SMALL), _p(str(v), BODY)] for k, v in conditions.items()]
    t = Table(rows, colWidths=[1.6 * inch, 5.4 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    story.append(_p("Artifact hashes (SHA-256)", H2))
    flat: list[list] = []

    def walk(prefix: str, value) -> None:
        if isinstance(value, dict):
            for k2, v2 in value.items():
                walk(f"{prefix}.{k2}" if prefix else str(k2), v2)
        else:
            flat.append([_p(prefix, SMALL), _p(str(value) if value else "—", MONO)])

    walk("", artifact_hashes)
    t = Table(flat, colWidths=[1.6 * inch, 5.4 * inch])
    story.append(t)
    story.append(_p("Consent as displayed", H2))
    story.append(_p(consent_text, SMALL))
    story.append(_p("Terms as displayed", H2))
    story.append(_p(terms_body, SMALL))
    story.append(_p("Event chain", H2))
    ev_rows = [["#", "When (UTC)", "Event", "Actor", "Hash"]]
    for e in events:
        ev_rows.append([str(e["sequence"]), e["occurred_at"], e["event_type"], e["actor_type"], e["event_hash"][:16] + "…"])
    t = Table(ev_rows, colWidths=[0.35 * inch, 1.5 * inch, 1.8 * inch, 0.8 * inch, 2.4 * inch])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                           ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2)]))
    story.append(t)
    story.append(_p(f"Chain head {event_chain_head}", MONO))
    story.append(Spacer(1, 6))
    story.append(_p(f"Verify this certificate at {verify_url} — paste the SHA-256 of this PDF. Any change to the proof PDF, a machine file or this certificate makes verification fail.", SMALL))
    doc.build(story)
    return buf.getvalue()
