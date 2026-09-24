"""Fixed and default copy -- PRD Appendix A. The honesty note is fixed and
not editable; the terms are the shop's editable default (counsel review
before launch); the consent text has a minimum that cannot be edited
below."""

DEFAULT_TERMS = """Artwork and approval. You are responsible for reviewing and approving the artwork, dimensions, thread colors, garment selection and placement shown on this proof before production begins. Production starts only after approval is received.

Color. Exact color matching is not guaranteed. Thread colors are matched as closely as reasonably possible using available thread inventories. Screen colors vary by device and lighting and are not a color standard. If exact color is critical, request a physical sew-out before approving.

Placement. Artwork placement may vary up to 0.5 inches in any direction.

Materials. Embroidery appearance varies with fabric. A design approved on one garment or fabric may look different on another. Approval covers the garment, color and fabric shown on this proof.

Spoilage. Spoilage of up to 2% is considered acceptable and non-compensable.

Digitizing. Digitizing charges are non-refundable once work has begun. Digitized files remain the property of the shop unless otherwise agreed in writing.

Timing. Quoted turnaround begins on approval and does not include time spent awaiting approval.

Changes after approval. Changes requested after approval may incur additional charges and may affect the delivery date."""

CONSENT_TEXT = ("By typing my name and selecting Approve, I confirm that I have reviewed this proof, that I intend to approve it "
                "electronically, and that I agree to the terms above. I understand this approval applies to the specific design "
                "version, colorway, garment, color, size, placement and fabric shown. I consent to receiving this record "
                "electronically and understand I may request a paper copy at any time by contacting the shop. A copy of this "
                "approval and its record will be emailed to me.")

HONESTY_NOTE = ("What this proof shows and what it doesn't. This image is generated from the actual embroidery file that will "
                "run on the machine, so the stitch count, dimensions and thread sequence are exact. But embroidery is thread on "
                "fabric: it stretches, compresses and catches light in ways a screen cannot reproduce, and fine detail behaves "
                "differently on different materials. Treat this as an accurate plan, not a photograph of the finished piece.")

# The consent text may be extended by a shop but never shortened below this.
CONSENT_MINIMUM = ("I confirm that I have reviewed this proof, that I intend to approve it electronically, and that I agree to the terms above.")

STABILIZER_ADVICE = {
    "standard": "One layer of medium tear-away backing. If the fabric stretches at all, use cut-away instead.",
    "stableWoven": "One layer of medium tear-away backing; for heavy fills, two layers or a medium cut-away.",
    "knit": "Medium cut-away backing (never tear-away on a knit). A light spray adhesive helps keep it from shifting.",
    "stretchKnit": "Medium or heavy cut-away backing, plus a water-soluble topping. Hoop the backing, float the garment, and don't stretch it in the hoop.",
    "terry": "Medium cut-away backing underneath and a water-soluble topping on top so the pile doesn't poke through between stitches.",
    "leatherOrVinyl": "Medium tear-away or a cut-away backing. Don't hoop the material itself: hoop the backing and float the piece with a light adhesive.",
    "structuredCap": "One layer of cap backing (a firm tear-away made for cap frames). Keep the frame's clamp tight so the panel can't shift.",
    "unstructuredCap": "A firm tear-away cap backing, and a soft cut-away if the panel is thin or stretchy. Take the crease out before framing.",
    "beanie": "Medium cut-away backing and a water-soluble topping. Float the beanie on hooped backing and don't stretch the knit.",
}

FABRIC_NAMES = {
    "standard": "Standard", "stableWoven": "Stable woven (twill, canvas, denim)", "knit": "Knit (t-shirt, polo)",
    "stretchKnit": "Stretch knit", "terry": "Terry / plush", "leatherOrVinyl": "Leather / vinyl",
    "structuredCap": "Structured cap", "unstructuredCap": "Unstructured cap", "beanie": "Knit beanie",
}


# The machine formats offered for download, in the order PiperStitch's own
# editor offers them: the one most machines read first. Kept here so the
# production panel and the version panel cannot drift apart.
MACHINE_FILE_LABELS: list[tuple[str, str, str]] = [
    ("dst", "Tajima", "most commercial machines"),
    ("pes", "Brother / Baby Lock", ""),
    ("jef", "Janome", ""),
    ("exp", "Melco / Bernina", ""),
    ("vp3", "Husqvarna Viking / Pfaff", ""),
]
