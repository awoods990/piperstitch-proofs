"""Art triage -- the Readiness Report (PRD 5.2). Runs on every uploaded
file, before any digitizing: what the file is, what it will look like at
the requested size, and the embroidery risks that can be *measured* from
the artwork alone. Every finding carries a severity, a plain sentence, a
measurement and a suggested fix.

Phase 2 slice: file-level analysis (resolution, vector/raster,
transparency, colour count, gradients) and the measurable risks
(smallest feature width at size, colours vs needles, cap field). The
digitizer-level risks (satin width, counter closure) come from composing
the proof in Core, whose own readiness report is shown alongside.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageFilter, ImageOps

RASTER_TYPES = {"image/png", "image/jpeg", "image/webp", "image/tiff", "image/bmp", "image/gif"}
VECTOR_TYPES = {"image/svg+xml", "application/pdf", "application/postscript", "application/illustrator"}
MACHINE_TYPES = {"dst", "pes", "exp", "jef", "vp3"}


@dataclass
class Finding:
    code: str
    severity: str        # blocker | warning | note
    title: str
    message: str
    measurement: dict = field(default_factory=dict)
    suggested_fix: str = ""


@dataclass
class Report:
    kind: str                     # raster | vector | machine | unknown
    pixel_width: int = 0
    pixel_height: int = 0
    effective_ppi: Optional[float] = None
    is_vector: bool = False
    has_transparency: bool = False
    white_box: bool = False
    color_count: int = 0
    colorspace: str = ""
    gradient: bool = False
    min_feature_mm: Optional[float] = None
    findings: list[Finding] = field(default_factory=list)
    preview_png: Optional[bytes] = None

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    def to_json(self) -> str:
        d = {k: v for k, v in self.__dict__.items() if k not in ("findings", "preview_png")}
        d["findings"] = [f.__dict__ for f in self.findings]
        return json.dumps(d)


def sniff(data: bytes, filename: str) -> str:
    """Magic bytes first, extension second."""
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:2] == b"%!":
        return "application/postscript"
    head = data[:2048].lstrip()
    if head.startswith(b"<?xml") and b"<svg" in data[:4096] or head.startswith(b"<svg"):
        return "image/svg+xml"
    if ext in MACHINE_TYPES:
        return f"application/x-embroidery-{ext}"
    if ext == "heic":
        return "image/heic"
    if ext == "psd" or data[:4] == b"8BPS":
        return "image/vnd.adobe.photoshop"
    if ext == "ai":
        return "application/illustrator"
    return "application/octet-stream"


def _quantized_colors(img: Image.Image, max_colors: int = 32) -> tuple[int, bool]:
    """Distinct colours after quantization, ignoring near-transparent
    pixels and colours under 1% coverage; plus whether the image looks
    like a gradient/photo (many colours, none dominant)."""
    small = img.convert("RGBA")
    small.thumbnail((256, 256))
    px = [p for p in small.getdata() if p[3] > 64]
    if not px:
        return 0, False
    opaque = Image.new("RGB", (len(px), 1))
    opaque.putdata([p[:3] for p in px])
    q = opaque.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    counts = sorted(q.getcolors(len(px)) or [], reverse=True)
    total = sum(c for c, _ in counts)
    significant = [c for c, _ in counts if c / total >= 0.01]
    # Merge visually near-identical palette entries (anti-aliasing noise).
    palette = q.getpalette()
    kept: list[tuple] = []
    for count, idx in counts:
        if count / total < 0.01:
            continue
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        if any(sum(abs(a - b) for a, b in zip(rgb, k)) < 48 for k in kept):
            continue
        kept.append(rgb)
    gradient = len(significant) >= 12 and (counts[0][0] / total) < 0.35
    return max(1, len(kept)), gradient


def _white_box(img: Image.Image) -> bool:
    """A solid white (or near-white) border around opaque artwork."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    if w < 4 or h < 4:
        return False
    samples = []
    for x in range(0, w, max(1, w // 40)):
        samples += [rgba.getpixel((x, 0)), rgba.getpixel((x, h - 1))]
    for y in range(0, h, max(1, h // 40)):
        samples += [rgba.getpixel((0, y)), rgba.getpixel((w - 1, y))]
    opaque = [s for s in samples if s[3] > 200]
    if len(opaque) < len(samples) * 0.95:
        return False
    white = [s for s in opaque if min(s[:3]) > 235]
    return len(white) >= len(opaque) * 0.95


def _min_feature_mm(img: Image.Image, width_mm: float) -> Optional[float]:
    """The narrowest stroke in the artwork at the requested size: erode
    the foreground mask by 1 px steps until it disappears; the width of
    the narrowest feature is about twice the erosion depth at which a
    meaningful amount of foreground is lost."""
    rgba = img.convert("RGBA")
    rgba.thumbnail((600, 600))
    w, h = rgba.size
    if w < 8 or width_mm <= 0:
        return None
    mm_per_px = width_mm / w
    # Foreground: non-transparent and not near-white (a white box is background).
    mask = Image.new("L", (w, h), 0)
    src = rgba.load()
    dst = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = src[x, y]
            dst[x, y] = 255 if (a > 128 and min(r, g, b) < 235) else 0
    total = sum(1 for v in mask.getdata() if v)
    if total < 20:
        return None
    eroded = mask
    for depth in range(1, 40):
        eroded = eroded.filter(ImageFilter.MinFilter(3))
        remaining = sum(1 for v in eroded.getdata() if v)
        # The narrowest strokes vanish first; once 5% of the foreground
        # has gone, strokes of about 2*depth px have disappeared.
        if remaining < total * 0.95:
            return round(2 * depth * mm_per_px, 2)
    return None


def analyze(data: bytes, filename: str, *, requested_width_mm: float, needle_count: int = 12, is_cap: bool = False) -> Report:
    mime = sniff(data, filename)
    if mime.startswith("application/x-embroidery-"):
        r = Report(kind="machine")
        r.findings.append(Finding("machine_file", "note", "Machine file supplied", "This is already a stitch file. It will be shown as sent; the shop will check it sews at this size.",
                                  {"format": mime.rsplit("-", 1)[-1]}))
        return r
    if mime == "image/svg+xml":
        r = Report(kind="vector", is_vector=True, colorspace="sRGB")
        text = data.decode("utf-8", errors="ignore")
        fills = set(re.findall(r'fill\s*[:=]\s*["\']?(#[0-9a-fA-F]{3,8}|rgb\([^)]*\))', text))
        r.color_count = max(1, len(fills))
        if "<image" in text:
            r.findings.append(Finding("svg_embedded_image", "warning", "The SVG contains an embedded picture",
                                      "Part of this file is a bitmap inside an SVG wrapper; that part is not vector and will be traced.", {},
                                      "Ask for the original vector file (AI, EPS or a clean SVG)."))
        if "<text" in text:
            r.findings.append(Finding("svg_live_text", "warning", "Live text in the SVG", "Text is still text, not outlines. If the font isn't installed here it will render differently.", {},
                                      "Convert text to outlines before sending, or name the font."))
        if re.search(r"<(linear|radial)Gradient", text):
            r.gradient = True
            r.findings.append(Finding("gradient", "warning", "Gradient fill", "Thread cannot fade. A gradient becomes two or three flat colours or a blended fill.", {},
                                      "Choose the flat colours you want, or accept a blended fill."))
        _needles(r, needle_count)
        return r
    if mime == "application/pdf":
        r = Report(kind="vector", is_vector=True)
        head = data[:200_000]
        has_image = b"/Subtype /Image" in head or b"/Subtype/Image" in head
        has_paths = b" re\n" in head or b" c\n" in head or b" l\n" in head or b"/Type /Font" in head
        if has_image and not has_paths:
            r.is_vector = False
            r.kind = "raster"
            r.findings.append(Finding("pdf_is_scan", "warning", "This PDF is a scan, not vector art", "The page is a single picture inside a PDF; it will be traced like a photo.", {},
                                      "Ask for the original artwork file if one exists."))
        if b"/FontFile" not in head and b"/Type /Font" in head:
            r.findings.append(Finding("fonts_not_embedded", "warning", "Fonts may not be embedded", "Text in this PDF may shift if a font isn't embedded.", {}, "Convert text to outlines."))
        return r
    if mime in ("image/heic", "image/vnd.adobe.photoshop", "application/illustrator", "application/postscript", "application/octet-stream"):
        r = Report(kind="unknown")
        r.findings.append(Finding("unsupported_format", "blocker", "File type not supported yet",
                                  f"'{filename}' ({mime}) can't be read here yet. PNG, JPG, WEBP, TIFF, SVG, PDF and machine files are.", {"mime": mime},
                                  "Export a PNG at the largest size you have, or an SVG/PDF from the design program."))
        return r
    # Raster.
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001
        r = Report(kind="unknown")
        r.findings.append(Finding("unreadable", "blocker", "The file couldn't be opened", "It may be corrupt or mislabeled.", {}, "Re-export and try again."))
        return r
    img = ImageOps.exif_transpose(img)
    r = Report(kind="raster", pixel_width=img.width, pixel_height=img.height, colorspace="sRGB")
    icc = img.info.get("icc_profile")
    if icc and b"Display P3" in icc:
        r.colorspace = "Display P3 (converted to sRGB)"
    if requested_width_mm > 0:
        r.effective_ppi = round(img.width / (requested_width_mm / 25.4), 0)
        if r.effective_ppi < 150:
            r.findings.append(Finding("low_resolution", "blocker" if r.effective_ppi < 90 else "warning", "Resolution is low for this size",
                                      f"Your logo is {img.width:,} px wide. At {requested_width_mm / 25.4:.1f} in that is {r.effective_ppi:.0f} PPI — "
                                      + ("too low to trace cleanly." if r.effective_ppi < 90 else "workable, but edges will be soft."),
                                      {"pixel_width": img.width, "effective_ppi": r.effective_ppi, "requested_width_mm": requested_width_mm},
                                      "Send a larger image or a vector file; or reduce the finished size."))
        else:
            r.findings.append(Finding("resolution_ok", "note", "Resolution is fine", f"Your logo is {img.width:,} px wide. At {requested_width_mm / 25.4:.1f} in that is {r.effective_ppi:.0f} PPI — good.",
                                      {"pixel_width": img.width, "effective_ppi": r.effective_ppi}))
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    r.has_transparency = alpha.getextrema()[0] < 255
    r.white_box = _white_box(img)
    if r.white_box:
        r.findings.append(Finding("white_box", "note", "White box behind the logo", "There's a solid white background around the artwork. We'll remove it so only the logo is stitched.", {}))
    r.color_count, r.gradient = _quantized_colors(img)
    r.findings.append(Finding("color_count", "note", f"We count {r.color_count} distinct colour{'s' if r.color_count != 1 else ''}",
                              "Each colour is a thread stop; stops drive cost and run time.", {"color_count": r.color_count}))
    if r.gradient:
        r.findings.append(Finding("gradient", "warning", "Gradient or photographic shading", "Thread cannot fade. Smooth shading becomes a few flat colours.", {},
                                  "Choose the flat colours you want the design reduced to."))
    r.min_feature_mm = _min_feature_mm(img, requested_width_mm) if requested_width_mm > 0 else None
    if r.min_feature_mm is not None:
        if r.min_feature_mm < 1.0:
            r.findings.append(Finding("detail_too_fine", "warning", "Some detail is very fine at this size",
                                      f"The narrowest strokes are about {r.min_feature_mm:.1f} mm at {requested_width_mm / 25.4:.1f} in wide. Below about 1 mm, lines fill in or break.",
                                      {"min_feature_mm": r.min_feature_mm}, f"Raise the finished width to about {requested_width_mm * 1.0 / max(r.min_feature_mm, 0.2) / 25.4:.1f} in, simplify the fine detail, or accept the risk."))
        elif r.min_feature_mm < 2.0:
            r.findings.append(Finding("detail_fine", "note", "Fine detail present", f"The narrowest strokes are about {r.min_feature_mm:.1f} mm at this size — sewable, but small text may soften.",
                                      {"min_feature_mm": r.min_feature_mm}))
    if is_cap and requested_width_mm > 0:
        aspect = img.height / max(1, img.width)
        height_mm = requested_width_mm * aspect
        if height_mm > 57.2:
            r.findings.append(Finding("cap_height", "warning", "Too tall for a cap front",
                                      f"At {requested_width_mm / 25.4:.1f} in wide this design is {height_mm / 25.4:.2f} in tall; a structured cap's front sews about 2.25 in.",
                                      {"height_mm": round(height_mm, 1)}, "Reduce the width, or use a flat-panel cap."))
    _needles(r, needle_count)
    # A small preview for the report page.
    preview = rgba.copy()
    preview.thumbnail((800, 800))
    buf = io.BytesIO()
    preview.save(buf, format="PNG")
    r.preview_png = buf.getvalue()
    return r


def _needles(r: Report, needle_count: int) -> None:
    if r.color_count > needle_count > 0:
        r.findings.append(Finding("colors_vs_needles", "warning", "More colours than needles",
                                  f"{r.color_count} colours on a {needle_count}-needle machine means a mid-run rethread.", {"color_count": r.color_count, "needles": needle_count},
                                  "Reduce the palette, or plan the rethread."))
