"""Garment context and placement diagrams -- PRD "The proof artifact":
"the render composited onto the actual garment style and color ... at
correct relative size" and "measured callouts on a garment outline".

Phase 1 ships twelve *drawn* garment templates rather than photographs:
a silhouette with seams, shaded and tinted to the garment colour, with
named zones that carry a real-world scale. Honest and consistent (a
drawing is obviously a drawing -- principle 2), colourable to any garment
colour without a photo per colour, and the same code path a photo
template will use later (a photo template is a zone list plus an image).

Every template is defined in a 1000 × 1000 design box. A zone is a
rectangle in that box plus `mm_per_px` for the box -- the garment's real
chest width divided by the pixels it spans -- so a 55 mm logo lands at
55 mm relative to the garment. `curve` warps the render for a cap crown.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont


@dataclass
class Zone:
    id: str
    label: str
    # Centre of the zone in the 1000-box, and its extent.
    cx: float
    cy: float
    w: float
    h: float
    # Where measurements are taken from for the placement diagram.
    anchor_label: str = "shoulder seam"
    anchor_x: float = 0
    anchor_y: float = 0
    curve: float = 0.0   # 0 flat; >0 barrel (cap crown)


@dataclass
class Template:
    id: str
    name: str
    category: str
    # Real width of the garment across the widest drawn extent, mm.
    real_width_mm: float
    outline: list[tuple[float, float]]
    seams: list[list[tuple[float, float]]] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    default_zone: str = ""

    @property
    def mm_per_px(self) -> float:
        xs = [p[0] for p in self.outline]
        return self.real_width_mm / max(1.0, (max(xs) - min(xs)))

    def zone(self, zone_id: str) -> Zone:
        for z in self.zones:
            if z.id == zone_id:
                return z
        return self.zones[0]


def _tee(outline_scale=1.0) -> list[tuple[float, float]]:
    return [(300, 120), (400, 90), (450, 150), (550, 150), (600, 90), (700, 120), (860, 200), (930, 360), (820, 420), (790, 380), (790, 880),
            (210, 880), (210, 380), (180, 420), (70, 360), (140, 200)]


TEMPLATES: list[Template] = [
    Template("polo", "Polo shirt", "shirt", 560, _tee(), seams=[[(450, 150), (430, 300), (500, 330), (500, 150)], [(500, 150), (500, 330)], [(210, 380), (790, 380)]],
             zones=[Zone("left_chest", "Left chest", 640, 330, 130, 130, "shoulder seam", 640, 150), Zone("full_front", "Full front", 500, 480, 400, 300, "collar", 500, 200),
                    Zone("full_back", "Full back", 500, 420, 420, 320, "collar", 500, 150)], default_zone="left_chest"),
    Template("tshirt", "T-shirt", "shirt", 560, _tee(), seams=[[(430, 150), (500, 190), (570, 150)], [(210, 380), (790, 380)]],
             zones=[Zone("left_chest", "Left chest", 640, 330, 130, 130, "shoulder seam", 640, 150), Zone("full_front", "Full front", 500, 470, 420, 320, "collar", 500, 190),
                    Zone("full_back", "Full back", 500, 420, 420, 320, "collar", 500, 150)], default_zone="left_chest"),
    Template("hoodie", "Hooded sweatshirt", "shirt", 600, [(300, 130), (380, 60), (620, 60), (700, 130), (870, 210), (940, 380), (830, 440), (800, 400), (800, 900), (200, 900), (200, 400), (170, 440), (60, 380), (130, 210)],
             seams=[[(380, 60), (420, 200), (580, 200), (620, 60)], [(360, 700), (640, 700), (640, 820), (360, 820), (360, 700)], [(200, 400), (800, 400)]],
             zones=[Zone("left_chest", "Left chest", 650, 330, 130, 130, "shoulder seam", 650, 130), Zone("full_front", "Full front", 500, 470, 380, 220, "hood seam", 500, 200)], default_zone="left_chest"),
    Template("quarter_zip", "Quarter-zip pullover", "shirt", 580, _tee(), seams=[[(500, 130), (500, 420)], [(455, 110), (455, 130), (545, 130), (545, 110)], [(210, 380), (790, 380)]],
             zones=[Zone("left_chest", "Left chest", 650, 330, 130, 130, "shoulder seam", 650, 150)], default_zone="left_chest"),
    Template("jacket", "Jacket", "shirt", 620, [(280, 130), (400, 80), (500, 140), (600, 80), (720, 130), (890, 220), (950, 400), (840, 450), (810, 410), (810, 900), (190, 900), (190, 410), (160, 450), (50, 400), (110, 220)],
             seams=[[(500, 140), (500, 900)], [(400, 80), (440, 300)], [(600, 80), (560, 300)], [(190, 410), (810, 410)]],
             zones=[Zone("left_chest", "Left chest", 650, 320, 130, 130, "shoulder seam", 650, 140), Zone("full_back", "Full back", 500, 430, 440, 340, "collar", 500, 140)], default_zone="left_chest"),
    Template("button_down", "Button-down shirt", "shirt", 560, _tee(), seams=[[(500, 150), (500, 880)], [(450, 150), (500, 230), (550, 150)], [(210, 380), (790, 380)]],
             zones=[Zone("left_chest", "Left chest", 640, 330, 130, 130, "shoulder seam", 640, 150), Zone("cuff", "Cuff", 850, 400, 80, 60, "cuff edge", 900, 400)], default_zone="left_chest"),
    Template("structured_cap", "Structured cap", "cap", 300, [(180, 480), (230, 300), (330, 190), (500, 150), (670, 190), (770, 300), (820, 480), (960, 520), (900, 580), (600, 560), (400, 560), (100, 580), (40, 520)],
             seams=[[(500, 150), (500, 480)], [(330, 190), (400, 480)], [(670, 190), (600, 480)], [(180, 480), (820, 480)]],
             zones=[Zone("front", "Front panel", 500, 380, 300, 170, "bottom of the front panel", 500, 480, curve=0.35), Zone("side", "Left side", 280, 400, 120, 90, "front panel seam", 330, 400, curve=0.2)], default_zone="front"),
    Template("bucket_hat", "Bucket hat", "cap", 320, [(300, 420), (330, 240), (420, 180), (580, 180), (670, 240), (700, 420), (940, 520), (900, 580), (100, 580), (60, 520)],
             seams=[[(330, 240), (670, 240)], [(300, 420), (700, 420)]], zones=[Zone("front", "Front", 500, 340, 240, 130, "brim", 500, 420, curve=0.3)], default_zone="front"),
    Template("beanie", "Knit beanie", "cap", 260, [(200, 560), (200, 320), (260, 170), (400, 90), (600, 90), (740, 170), (800, 320), (800, 560), (500, 620)],
             seams=[[(200, 440), (500, 500), (800, 440)], [(200, 560), (500, 620), (800, 560)]], zones=[Zone("cuff", "Front cuff", 500, 515, 220, 90, "bottom edge", 500, 600, curve=0.25)], default_zone="cuff"),
    Template("towel", "Towel", "textile", 700, [(120, 100), (880, 100), (880, 900), (120, 900)],
             seams=[[(120, 220), (880, 220)], [(120, 780), (880, 780)]], zones=[Zone("border", "Above the border", 500, 720, 400, 100, "hem", 500, 900), Zone("corner", "Corner", 700, 300, 220, 160, "top edge", 700, 100),
                                                                              Zone("center", "Centre", 500, 500, 500, 360, "top edge", 500, 100)], default_zone="border"),
    Template("tote", "Tote bag", "bag", 400, [(200, 300), (800, 300), (820, 900), (180, 900)],
             seams=[[(330, 300), (330, 120), (400, 120)], [(670, 300), (670, 120), (600, 120)], [(400, 120), (600, 120)]], zones=[Zone("front", "Front", 500, 600, 400, 380, "top edge", 500, 300)], default_zone="front"),
    Template("apron", "Apron", "textile", 620, [(370, 100), (630, 100), (630, 340), (900, 340), (900, 920), (100, 920), (100, 340), (370, 340)],
             seams=[[(370, 100), (300, 20)], [(630, 100), (700, 20)], [(370, 340), (630, 340)]], zones=[Zone("chest", "Chest (bib)", 500, 230, 200, 150, "top edge of bib", 500, 100), Zone("center", "Centre", 500, 640, 500, 380, "waist seam", 500, 340)], default_zone="chest"),
]

TEMPLATE_BY_ID = {t.id: t for t in TEMPLATES}

GARMENT_COLORS: list[tuple[str, str]] = [
    ("White", "#f4f4f2"), ("Ash", "#d5d5d0"), ("Sport grey", "#9b9d9f"), ("Charcoal", "#4b4e52"), ("Black", "#1c1d20"),
    ("Navy", "#1f2d4d"), ("Royal", "#2b52a8"), ("Light blue", "#a9c7e6"), ("Red", "#b0202a"), ("Maroon", "#6b1f2c"),
    ("Forest", "#1f4a34"), ("Kelly", "#2c8a4b"), ("Gold", "#e0b332"), ("Orange", "#e0682a"), ("Purple", "#4d2a7a"),
    ("Khaki", "#c8b48c"), ("Brown", "#5a3d2b"), ("Pink", "#e8a5c1"),
]
GARMENT_COLOR_HEX = {name.lower(): hex_ for name, hex_ in GARMENT_COLORS}


def color_hex(name_or_hex: str) -> str:
    s = (name_or_hex or "").strip()
    if s.startswith("#") and len(s) == 7:
        return s.lower()
    return GARMENT_COLOR_HEX.get(s.lower(), "#9b9d9f")


def _rgb(hex_: str) -> tuple[int, int, int]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def _mix(c: tuple, k: float, toward=(0, 0, 0)) -> tuple:
    return tuple(int(round(c[i] * (1 - k) + toward[i] * k)) for i in range(3))


def _font(size: int):
    for name in ("Helvetica.ttc", "Arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_garment(template: Template, garment_hex: str, size: int = 1000, background=(247, 243, 236)) -> Image.Image:
    """The tinted, shaded silhouette with seams, no logo yet."""
    s = size / 1000.0
    base = _rgb(garment_hex)
    img = Image.new("RGB", (size, size), background)
    # Soft shadow under the garment.
    shadow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(shadow).polygon([(x * s + 6 * s, y * s + 10 * s) for x, y in template.outline], fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * s))
    img.paste(_mix(background, 0.35), (0, 0), shadow)
    # The garment, with a vertical shading gradient and fabric grain.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon([(x * s, y * s) for x, y in template.outline], fill=255)
    fabric = Image.new("RGB", (size, size), base)
    grad = Image.linear_gradient("L").resize((size, size))            # top black → bottom white
    dark = Image.new("RGB", (size, size), _mix(base, 0.22))
    light = Image.new("RGB", (size, size), _mix(base, 0.10, toward=(255, 255, 255)))
    fabric = Image.composite(dark, light, grad.point(lambda v: int(v * 0.55)))
    # Side shading: darker toward the outer edges.
    hgrad = Image.linear_gradient("L").rotate(90, expand=True).resize((size, size))
    hshade = hgrad.point(lambda v: int(abs(v - 128) * 0.9))
    fabric = Image.composite(Image.new("RGB", (size, size), _mix(base, 0.3)), fabric, hshade)
    # Grain.
    import random
    rnd = random.Random(7)
    grain = Image.new("L", (size, size), 128)
    px = grain.load()
    for y in range(0, size, 2):
        for x in range(0, size, 2):
            v = 128 + rnd.randint(-9, 9)
            px[x, y] = v
    grain = grain.resize((size, size), Image.BILINEAR)
    fabric = Image.blend(fabric, Image.merge("RGB", (grain, grain, grain)), 0.06)
    img.paste(fabric, (0, 0), mask)
    d = ImageDraw.Draw(img)
    outline = _mix(base, 0.45)
    d.polygon([(x * s, y * s) for x, y in template.outline], outline=outline, width=max(1, int(2 * s)))
    for seam in template.seams:
        d.line([(x * s, y * s) for x, y in seam], fill=_mix(base, 0.35), width=max(1, int(2 * s)), joint="curve")
        # Stitch dashes beside the seam.
        d.line([(x * s + 3 * s, y * s + 3 * s) for x, y in seam], fill=_mix(base, 0.12, toward=(255, 255, 255)), width=max(1, int(1 * s)))
    return img


def _barrel(render: Image.Image, amount: float) -> Image.Image:
    """A gentle horizontal barrel warp for a cap crown: the middle bows
    down, the sides lift, and the ends compress -- via a mesh transform."""
    w, h = render.size
    if amount <= 0 or w < 8:
        return render
    cols = 16
    mesh = []
    for i in range(cols):
        x0, x1 = w * i / cols, w * (i + 1) / cols
        def f(x):
            t = (x / w) * 2 - 1
            return (1 - amount * 0.5 * t * t)   # vertical scale factor
        def yoff(x):
            t = (x / w) * 2 - 1
            return amount * 0.12 * h * (1 - t * t)  # centre dips down
        # destination box and the source quad it samples
        dx0, dx1 = int(x0), int(x1)
        sy0 = 0
        sy1 = h
        mesh.append(((dx0, 0, dx1, h), (x0, sy0, x0, sy1, x1, sy1, x1, sy0)))
    warped = render.transform(render.size, Image.MESH, mesh, Image.BILINEAR)
    # Apply the vertical squeeze/offset per column by resampling rows.
    out = Image.new("RGBA", (w, h + int(amount * 0.12 * h) + 2), (0, 0, 0, 0))
    col_w = max(1, w // 64)
    for x in range(0, w, col_w):
        strip = warped.crop((x, 0, min(w, x + col_w), h))
        t = ((x + col_w / 2) / w) * 2 - 1
        scale = 1 - amount * 0.5 * t * t
        new_h = max(1, int(h * scale))
        strip = strip.resize((strip.width, new_h), Image.BILINEAR)
        out.paste(strip, (x, int(amount * 0.12 * h * (1 - t * t))), strip)
    return out


def _layout(render_w: int, render_h: int, render_ppm: float, template: Template, zone: Zone, offsets_mm: tuple[float, float], size: int = 1000) -> dict:
    """Where the design lands on the garment box, and how the composite
    is cropped -- shared by `composite` and `design_box` so the page's
    drag handle and the picture can never disagree."""
    s = size / 1000.0
    px_per_mm = s / template.mm_per_px
    scale = px_per_mm / render_ppm
    w, h = max(1, int(render_w * scale)), max(1, int(render_h * scale))
    cx = zone.cx * s + offsets_mm[1] * px_per_mm
    cy = zone.cy * s + offsets_mm[0] * px_per_mm
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    xs = [p[0] * s for p in template.outline]
    ys = [p[1] * s for p in template.outline]
    m = 40 * s
    left, top = max(0, int(min(xs + [x0]) - m)), max(0, int(min(ys + [y0]) - m))
    right, bottom = min(size, int(max(xs + [x0 + w]) + m)), min(size, int(max(ys + [y0 + h]) + m))
    return {"scale": scale, "w": w, "h": h, "x0": x0, "y0": y0, "crop": (left, top, right, bottom), "px_per_mm": px_per_mm}


def design_box(render_w: int, render_h: int, render_ppm: float, template: Template, zone: Zone, offsets_mm: tuple[float, float], size: int = 1000) -> dict:
    """The design's rectangle in the finished (cropped) mockup's pixels,
    plus the mockup's pixels-per-mm, for dragging it on the page."""
    L = _layout(render_w, render_h, render_ppm, template, zone, offsets_mm, size)
    left, top, right, bottom = L["crop"]
    return {"x": L["x0"] - left, "y": L["y0"] - top, "w": L["w"], "h": L["h"], "img_w": right - left, "img_h": bottom - top, "px_per_mm": L["px_per_mm"]}


def composite(render_png: bytes, render_ppm: float, template: Template, zone: Zone, garment_hex: str,
              size: int = 1000, offsets_mm: tuple[float, float] = (0, 0)) -> bytes:
    """The stitch render placed on the garment at true relative scale.
    `render_ppm` is the render's pixels per mm; the render's own padding
    is transparent-ish sand, so it is keyed out to the garment."""
    s = size / 1000.0
    img = draw_garment(template, garment_hex, size)
    render = Image.open(io.BytesIO(render_png)).convert("RGBA")
    # Key out the render's sand background so only thread shows.
    bg = (247, 243, 236)
    px = render.load()
    for y in range(render.height):
        for x in range(render.width):
            r, g, b, a = px[x, y]
            if abs(r - bg[0]) < 10 and abs(g - bg[1]) < 10 and abs(b - bg[2]) < 10:
                px[x, y] = (r, g, b, 0)
    L = _layout(render.width, render.height, render_ppm, template, zone, offsets_mm, size)
    render = render.resize((L["w"], L["h"]), Image.LANCZOS)
    if zone.curve > 0:
        render = _barrel(render, zone.curve)
    # Embroidery sits proud of the cloth: a soft drop shadow.
    shadow = Image.new("RGBA", render.size, (0, 0, 0, 0))
    alpha = render.split()[3].point(lambda v: int(v * 0.55))
    shadow.paste((20, 15, 10, 255), (0, 0), alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2.5 * s))
    x0, y0 = L["x0"], L["y0"]
    img.paste(shadow, (x0 + int(2 * s), y0 + int(3 * s)), shadow)
    img.paste(render, (x0, y0), render)
    # Crop to the garment (plus a margin) so a cap doesn't float in a
    # field of sand on a phone.
    img = img.crop(L["crop"])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def placement_diagram(template: Template, zone: Zone, design_w_mm: float, design_h_mm: float, down_mm: float, across_mm: float,
                      units: str = "imperial", size: int = 1000) -> bytes:
    """A garment outline with the design box and two measured callouts:
    down from the zone's anchor and across from the centre line."""
    s = size / 1000.0
    img = Image.new("RGB", (size, size), (255, 255, 254))
    d = ImageDraw.Draw(img)
    ink = (60, 70, 85)
    d.polygon([(x * s, y * s) for x, y in template.outline], outline=ink, width=max(1, int(3 * s)), fill=(247, 243, 236))
    for seam in template.seams:
        d.line([(x * s, y * s) for x, y in seam], fill=(150, 156, 165), width=max(1, int(2 * s)))
    px_per_mm = s / template.mm_per_px
    cx = zone.cx * s + across_mm * px_per_mm
    cy = zone.cy * s + down_mm * px_per_mm
    bw, bh = design_w_mm * px_per_mm, design_h_mm * px_per_mm
    box = (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
    d.rectangle(box, outline=(44, 110, 143), width=max(1, int(3 * s)))
    d.line([(box[0], box[1]), (box[2], box[3])], fill=(44, 110, 143), width=max(1, int(1 * s)))
    d.line([(box[2], box[1]), (box[0], box[3])], fill=(44, 110, 143), width=max(1, int(1 * s)))

    def fmt(mm: float) -> str:
        return f"{mm / 25.4:.2f} in" if units == "imperial" else f"{mm:.0f} mm"

    font = _font(int(26 * s))
    red = (176, 0, 32)
    # From the anchor to the nearest edge of the design: down to its top
    # when the anchor is above it (a shoulder seam), up to its bottom when
    # the anchor is below (a cap's front-panel edge, a hem).
    ax, ay = zone.anchor_x * s, zone.anchor_y * s
    below = ay <= box[1]
    edge = box[1] if below else box[3]
    d.line([(ax, ay), (ax, edge)], fill=red, width=max(1, int(3 * s)))
    d.line([(ax - 12 * s, ay), (ax + 12 * s, ay)], fill=red, width=max(1, int(3 * s)))
    d.line([(ax - 12 * s, edge), (ax + 12 * s, edge)], fill=red, width=max(1, int(3 * s)))
    gap = abs(edge - ay) / px_per_mm
    d.text((ax + 16 * s, (ay + edge) / 2 - 14 * s), f"{fmt(gap)} {'below' if below else 'above'} {zone.anchor_label}", fill=red, font=font)
    # Across from the centre line to the design's centre.
    centre_x = 500 * s
    d.line([(centre_x, 120 * s), (centre_x, 900 * s)], fill=(180, 186, 195), width=max(1, int(2 * s)))
    across_total = abs(cx - centre_x) / px_per_mm
    if across_total >= 1:
        yline = box[3] + 30 * s
        d.line([(centre_x, yline), (cx, yline)], fill=red, width=max(1, int(3 * s)))
        d.line([(centre_x, yline - 12 * s), (centre_x, yline + 12 * s)], fill=red, width=max(1, int(3 * s)))
        d.line([(cx, yline - 12 * s), (cx, yline + 12 * s)], fill=red, width=max(1, int(3 * s)))
        side = "left" if cx > centre_x else "right"   # wearer's left is viewer's right
        d.text((min(cx, centre_x), yline + 16 * s), f"{fmt(across_total)} {side} of centre", fill=red, font=font)
    else:
        d.text((cx + 12 * s, box[3] + 14 * s), "centred", fill=red, font=font)
    d.text((box[0], box[1] - 34 * s), f"{fmt(design_w_mm)} × {fmt(design_h_mm)}", fill=(44, 110, 143), font=font)
    d.text((20 * s, 20 * s), f"{template.name} — {zone.label}", fill=ink, font=font)
    d.text((20 * s, 950 * s), "Placement may vary up to 0.5 in in any direction.", fill=(120, 126, 135), font=_font(int(22 * s)))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def measured_note(template: Template, zone: Zone, design_h_mm: float, down_mm: float, across_mm: float, units: str = "imperial") -> str:
    """The placement sentence for the proof and run ticket."""
    mm_per_box_px = template.mm_per_px
    cy = zone.cy + down_mm / mm_per_box_px
    cx = zone.cx + across_mm / mm_per_box_px
    half_h = design_h_mm / 2
    below = zone.anchor_y <= cy - half_h / mm_per_box_px
    gap = abs((cy - zone.anchor_y) * mm_per_box_px) - half_h
    centre_from_centre = abs(cx - 500) * mm_per_box_px

    def fmt(mm: float) -> str:
        return f"{mm / 25.4:.1f} in" if units == "imperial" else f"{mm:.0f} mm"
    side = "left" if cx > 500 else "right"
    parts = [f"{'top' if below else 'bottom'} of design {fmt(max(0, gap))} {'below' if below else 'above'} {zone.anchor_label}"]
    parts.append(f"centre {fmt(centre_from_centre)} {side} of centre" if centre_from_centre >= 1 else "centred")
    return ", ".join(parts)
