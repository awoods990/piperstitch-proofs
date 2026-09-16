"""The customer-facing emails, written once: a plain-text body and an
HTML twin with the shop's name and colour and one clear button. Every
email says who it's from (the shop), what it's about (the job by name),
what to do (one action), and how long the link lasts.
"""

from __future__ import annotations

from html import escape

from .db import Account, Contact, Proof


def _wrap(account: Account, title: str, paragraphs: list[str], button_text: str, button_url: str, footnote: str) -> str:
    color = account.brand_color or "#2c6e8f"
    shop = escape(account.shop_name or "Your embroiderer")
    body = "".join(f'<p style="margin:0 0 14px;font-size:16px;line-height:1.5;color:#12161c">{escape(p)}</p>' for p in paragraphs)
    return f"""<!doctype html><html><body style="margin:0;background:#f7f3ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f7f3ec;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="560" cellspacing="0" cellpadding="0" style="max-width:560px;width:100%;background:#fffffe;border-radius:14px;border-top:5px solid {color}">
<tr><td style="padding:26px 28px 6px"><div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#8b9099">{shop}</div>
<h1 style="margin:6px 0 16px;font-size:22px;line-height:1.25;color:#12161c">{escape(title)}</h1>{body}
<p style="margin:22px 0"><a href="{escape(button_url)}" style="display:inline-block;background:{color};color:#ffffff;text-decoration:none;font-weight:700;font-size:16px;padding:14px 26px;border-radius:10px">{escape(button_text)}</a></p>
<p style="margin:0 0 6px;font-size:13px;color:#5d6472">Or copy this link into your browser:<br><a href="{escape(button_url)}" style="color:{color};word-break:break-all">{escape(button_url)}</a></p>
<p style="margin:16px 0 0;font-size:13px;color:#8b9099">{escape(footnote)}</p></td></tr>
<tr><td style="padding:14px 28px 24px;font-size:12px;color:#8b9099;border-top:1px solid #e6e1d8">Sent by {shop} using PiperStitch Proofs. Replies go to {escape(account.reply_to_email or shop)}.</td></tr>
</table></td></tr></table></body></html>"""


def _text(account: Account, greeting: str, paragraphs: list[str], button_text: str, url: str, footnote: str) -> str:
    shop = account.shop_name or "Your embroiderer"
    return f"{greeting}\n\n" + "\n\n".join(paragraphs) + f"\n\n{button_text}:\n{url}\n\n{footnote}\n\n— {shop}" + (f"\n{account.reply_to_email}" if account.reply_to_email else "")


def intake_request(account: Account, contact: Contact, proof: Proof, url: str, *, note: str, expires: str) -> tuple[str, str, str]:
    shop = account.shop_name
    first = (contact.display_name or "").split(" ")[0] or "there"
    paragraphs = [
        f"Thanks for choosing {shop} for “{proof.title}”. To get started we need your artwork and a few details about the job.",
        "The best file is the original from whoever designed your logo — a vector file (AI, EPS, SVG, PDF) or the largest PNG or JPG you have. If all you have is a photo of a card or a shirt, send that; we'll tell you right away whether it will work.",
        "The form takes about two minutes and asks what the logo is going on, how big it should be, where, and when you need it.",
    ]
    if note.strip():
        paragraphs.append(f"A note from {shop}: {note.strip()}")
    footnote = f"This link is just for you and works until {expires}. No account or password needed."
    subject = f"{shop} needs your artwork for “{proof.title}”"
    text = _text(account, f"Hi {first},", paragraphs, "Upload your artwork", url, footnote)
    html = _wrap(account, f"Send us your artwork for “{proof.title}”", [f"Hi {first},"] + paragraphs, "Upload your artwork", url, footnote)
    return subject, text, html


def proof_ready(account: Account, contact: Contact, proof: Proof, url: str, *, version: int, note: str, respond_by: str) -> tuple[str, str, str]:
    shop = account.shop_name
    first = (contact.display_name or "").split(" ")[0] or "there"
    paragraphs = [
        f"Your embroidery proof for “{proof.title}” is ready to look at" + (f" — this is version {version}." if version > 1 else "."),
        "It shows your logo exactly as it will sew: the real stitches, the thread colours in order, the finished size, and where it sits on the garment. Please zoom in and check the details — especially any small text.",
        "If it looks right, tap Approve and type your name. If something needs changing, tap “Request changes” and tell us what — you can drop a pin on the spot you mean. Nothing gets sewn until you approve.",
    ]
    if note.strip():
        paragraphs.append(f"A note from {shop}: {note.strip()}")
    footnote = f"Please respond by {respond_by}. The link is just for you; no account needed."
    subject = f"Your proof from {shop} is ready — “{proof.title}”" + (f" (v{version})" if version > 1 else "")
    text = _text(account, f"Hi {first},", paragraphs, "Review and approve your proof", url, footnote)
    html = _wrap(account, f"Your proof is ready: “{proof.title}”", [f"Hi {first},"] + paragraphs, "Review and approve your proof", url, footnote)
    return subject, text, html


def proof_resend(account: Account, contact: Contact, proof: Proof, url: str) -> tuple[str, str, str]:
    shop = account.shop_name
    first = (contact.display_name or "").split(" ")[0] or "there"
    paragraphs = [f"Here's a fresh link to your embroidery proof for “{proof.title}” from {shop}. Any earlier link you had has been replaced by this one."]
    footnote = "The link is just for you; no account needed."
    subject = f"New link to your proof from {shop} — “{proof.title}”"
    return subject, _text(account, f"Hi {first},", paragraphs, "Open your proof", url, footnote), _wrap(account, f"A new link to your proof", [f"Hi {first},"] + paragraphs, "Open your proof", url, footnote)


def approved(account: Account, contact: Contact, proof: Proof, cert_url: str, *, version: int, approved_at: str, sha: str) -> tuple[str, str, str]:
    shop = account.shop_name
    first = (contact.display_name or "").split(" ")[0] or "there"
    paragraphs = [
        f"Thank you — your approval of “{proof.title}” (version {version}) was recorded on {approved_at[:10]}. {shop} is clear to start sewing.",
        "Your Certificate of Approval records exactly what you approved: the design, the size, the thread colours, the garment and placement, and a fingerprint of the machine file itself. Keep the link; it never expires.",
    ]
    footnote = f"Certificate SHA-256: {sha}"
    subject = f"Approved: “{proof.title}” — your certificate from {shop}"
    return subject, _text(account, f"Hi {first},", paragraphs, "View your Certificate of Approval", cert_url, footnote), _wrap(account, "Your approval is recorded", [f"Hi {first},"] + paragraphs, "View your Certificate of Approval", cert_url, footnote)


def reminder(account: Account, contact: Contact, proof: Proof, url: str, *, cadence: str) -> tuple[str, str, str]:
    shop = account.shop_name
    first = (contact.display_name or "").split(" ")[0] or "there"
    if cadence == "intake":
        paragraphs = [f"Just a nudge from {shop}: we're waiting on your artwork for “{proof.title}” before we can start. It takes a couple of minutes on your phone."]
        button, subject, title = "Upload your artwork", f"Still need your artwork for “{proof.title}”", "We're waiting on your artwork"
    else:
        paragraphs = [f"Your proof for “{proof.title}” from {shop} is waiting for a yes — or for changes. It takes a minute on your phone, and nothing gets sewn until you approve."]
        button, subject, title = "Review your proof", f"Your proof from {shop} is waiting — “{proof.title}”", "Your proof is waiting for you"
    footnote = "If you've already replied to the shop directly, please ignore this."
    return subject, _text(account, f"Hi {first},", paragraphs, button, url, footnote), _wrap(account, title, [f"Hi {first},"] + paragraphs, button, url, footnote)
