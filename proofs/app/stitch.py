"""What Proofs derives from a Core `digitize` response: the design hash,
the thread stop list, the finished dimensions, and the deterministic
renders (PRD "The proof artifact", "Integration with PiperStitch Core").

The plan is Core's wire form: one row per command, `[code, x, y]` in
design millimetres rounded to 0.01 mm. Codes: 0 stitch, 1 jump, 2 colour
change, 3 trim, 4 stop, 5 end. Non-movement rows repeat the last position.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter

STITCH, JUMP, COLOR_CHANGE, TRIM, STOP, END = 0, 1, 2, 3, 4, 5


@dataclass
class Stop:
    stop_number: int
    thread_brand: str
    thread_code: str
    thread_name: str
    hex: str
    stitch_count: int


@dataclass
class Analysis:
    design_hash: str
    stitch_count: int
    color_change_count: int
    trim_count: int
    width_mm: float
    height_mm: float
    min_x: float
    min_y: float
    estimated_run_seconds: float
    stops: list[Stop] = field(default_factory=list)


def design_hash(commands: list[list[float]]) -> str:
    """SHA-256 over exactly the PRD's pre-image: the ordered stitch blocks
    with coordinates in 0.1 mm, the stop boundaries and their order, the
    trim and jump commands, and the finished bounding dimensions. Nothing
    about colours, names or settings -- a palette change is not a design
    change (that is caught by the conditions snapshot instead)."""
    h = hashlib.sha256()
    h.update(b"PSDH1\n")
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for code, x, y in commands:
        code = int(code)
        if code in (STITCH, JUMP):
            xi, yi = int(round(x * 10)), int(round(y * 10))
            min_x, max_x = min(min_x, xi), max(max_x, xi)
            min_y, max_y = min(min_y, yi), max(max_y, yi)
            h.update(f"{'S' if code == STITCH else 'J'}{xi},{yi}\n".encode())
        elif code == COLOR_CHANGE:
            h.update(b"C\n")
        elif code == TRIM:
            h.update(b"T\n")
        elif code == STOP:
            h.update(b"P\n")
        elif code == END:
            h.update(b"E\n")
    if min_x != float("inf"):
        h.update(f"W{max_x - min_x}H{max_y - min_y}\n".encode())
    return h.hexdigest()


def analyze(digitized: dict) -> Analysis:
    commands = digitized["plan"]["commands"]
    colors = digitized.get("colors", [])
    stats = digitized.get("stats", {})
    bounds = stats.get("bounds") or {}
    # Stitches per colour block, in order.
    per_block: list[int] = [0]
    for code, _x, _y in commands:
        code = int(code)
        if code == STITCH:
            per_block[-1] += 1
        elif code == COLOR_CHANGE:
            per_block.append(0)
    stops: list[Stop] = []
    for i, count in enumerate(per_block):
        c = colors[i] if i < len(colors) else (colors[-1] if colors else {"name": "Thread", "rgb": {"r": 0, "g": 0, "b": 0}})
        rgb = c.get("rgb") or {}
        stops.append(Stop(
            stop_number=i + 1,
            thread_brand=c.get("brand") or "",
            thread_code=c.get("catalogNumber") or "",
            thread_name=c.get("name") or f"Colour {i + 1}",
            hex="#%02x%02x%02x" % (int(rgb.get("r", 0)), int(rgb.get("g", 0)), int(rgb.get("b", 0))),
            stitch_count=count,
        ))
    width = float(bounds.get("maxX", 0)) - float(bounds.get("minX", 0))
    height = float(bounds.get("maxY", 0)) - float(bounds.get("minY", 0))
    return Analysis(
        design_hash=design_hash(commands),
        stitch_count=int(stats.get("stitchCount", sum(per_block))),
        color_change_count=int(stats.get("colorChangeCount", max(0, len(per_block) - 1))),
        trim_count=int(stats.get("trimCount", 0)),
        width_mm=round(width, 2),
        height_mm=round(height, 2),
        min_x=float(bounds.get("minX", 0)),
        min_y=float(bounds.get("minY", 0)),
        estimated_run_seconds=float(stats.get("estimatedRunSeconds", 0)),
        stops=stops,
    )


# --- rendering -----------------------------------------------------------------

def render_png(digitized: dict, *, pixels_per_mm: float = 12.0, padding_mm: float = 4.0,
               background: tuple = (247, 243, 236), max_width_px: int = 2400) -> bytes:
    """A deterministic stitch render: every stitch as a stroked line with
    round caps in its thread colour, a slightly darker core line for
    texture and a faint drop shadow so the embroidery sits proud of the
    cloth. Drawn at 2x and downsampled for anti-aliasing. Same input,
    same Pillow, same bytes -- the render is hashed at creation."""
    analysis = analyze(digitized)
    commands = digitized["plan"]["commands"]
    colors = [s.hex for s in analysis.stops]
    width_mm = max(analysis.width_mm, 1) + padding_mm * 2
    height_mm = max(analysis.height_mm, 1) + padding_mm * 2
    ppm = min(pixels_per_mm, max_width_px / width_mm)
    scale = 2
    W, H = int(width_mm * ppm * scale), int(height_mm * ppm * scale)
    img = Image.new("RGB", (W, H), background)
    draw = ImageDraw.Draw(img)

    def px(x: float, y: float) -> tuple[float, float]:
        return ((x - analysis.min_x + padding_mm) * ppm * scale, (y - analysis.min_y + padding_mm) * ppm * scale)

    thread_w = max(1, int(0.48 * ppm * scale))   # polyester 40wt flattens to about this on cloth
    shadow_w = max(1, int(0.42 * ppm * scale))
    shadow_off = max(1, int(0.25 * ppm * scale))
    # Pass 1: shadow. Pass 2: thread. Pass 3: highlight core.
    segments: list[tuple[int, tuple, tuple]] = []
    block = 0
    last = None
    for code, x, y in commands:
        code = int(code)
        if code == STITCH:
            p = px(x, y)
            if last is not None:
                segments.append((block, last, p))
            last = p
        elif code == JUMP:
            last = px(x, y)
        elif code == COLOR_CHANGE:
            block += 1
            last = None
        elif code in (TRIM, STOP):
            last = None
    shadow = tuple(max(0, c - 70) for c in background)
    for _b, a, b in segments:
        draw.line([(a[0] + shadow_off, a[1] + shadow_off), (b[0] + shadow_off, b[1] + shadow_off)], fill=shadow, width=shadow_w)
    # Thread and highlight are drawn together, stitch by stitch, in sewing
    # order -- so underlay, travel runs and earlier colours end up under the
    # stitching that covers them, the way the finished piece looks. Jumps
    # (the threads the embroiderer trims) are never drawn.
    core_w = max(1, thread_w // 3)
    palette = {}
    for b_idx in set(b for b, _a, _c in segments):
        hex_color = colors[b_idx] if b_idx < len(colors) else "#000000"
        rgb = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        palette[b_idx] = (rgb, tuple(min(255, c + 45) for c in rgb))
    for b_idx, a, b in segments:
        rgb, hi = palette[b_idx]
        draw.line([a, b], fill=rgb, width=thread_w)
        draw.ellipse([a[0] - thread_w / 2, a[1] - thread_w / 2, a[0] + thread_w / 2, a[1] + thread_w / 2], fill=rgb)
        draw.line([a, b], fill=hi, width=core_w)
    img = img.resize((W // scale, H // scale), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def render_pixels_per_mm(render_png: bytes, analysis: Analysis, padding_mm: float = 4.0) -> float:
    """The scale `render_png` was drawn at (its width spans the design plus padding)."""
    im = Image.open(io.BytesIO(render_png))
    return im.width / (max(analysis.width_mm, 1) + padding_mm * 2)


def fit_into(png: bytes, size: tuple[int, int], background: tuple = (247, 243, 236)) -> bytes:
    """The render centred on a canvas of `size` (hero 1200x630, social 1080x1080)."""
    src = Image.open(io.BytesIO(png)).convert("RGB")
    canvas = Image.new("RGB", size, background)
    margin = 0.08
    box_w, box_h = int(size[0] * (1 - 2 * margin)), int(size[1] * (1 - 2 * margin))
    ratio = min(box_w / src.width, box_h / src.height)
    resized = src.resize((max(1, int(src.width * ratio)), max(1, int(src.height * ratio))), Image.LANCZOS)
    canvas.paste(resized, ((size[0] - resized.width) // 2, (size[1] - resized.height) // 2))
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def difference_png(a_png: bytes, b_png: bytes) -> bytes:
    """Where two renders differ: both scaled to the larger canvas,
    unchanged pixels faded to grey, changed pixels in red -- the
    'difference' view of the version compare (PRD 5.5)."""
    from PIL import ImageChops
    a = Image.open(io.BytesIO(a_png)).convert("RGB")
    b = Image.open(io.BytesIO(b_png)).convert("RGB")
    size = (max(a.width, b.width), max(a.height, b.height))
    bg = (247, 243, 236)
    ca, cb = Image.new("RGB", size, bg), Image.new("RGB", size, bg)
    ca.paste(a, ((size[0] - a.width) // 2, (size[1] - a.height) // 2))
    cb.paste(b, ((size[0] - b.width) // 2, (size[1] - b.height) // 2))
    diff = ImageChops.difference(ca, cb).convert("L").point(lambda v: 255 if v > 40 else 0)
    faded = Image.blend(cb.convert("L").convert("RGB"), Image.new("RGB", size, bg), 0.6)
    red = Image.new("RGB", size, (176, 0, 32))
    out = Image.composite(red, faded, diff.filter(ImageFilter.MaxFilter(3)) if size[0] > 8 else diff)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
