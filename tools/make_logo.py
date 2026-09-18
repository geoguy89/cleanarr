"""Draw the Cleanarr mark - a mouth washed out with soap, swearing in symbols.

Unraid shows this beside the container, the phone uses it for the home screen,
and the browser uses it for the tab. All of it is written by hand with the
standard library, which is less trouble than adding Pillow to an image that
otherwise only needs ffmpeg and a speech model.

Every shape is drawn in a 256-unit design space and scaled to whatever size is
asked for, so one drawing serves every size rather than a second set of
hand-tuned numbers. Each shape returns coverage rather than being stamped down,
so overlaps blend and nothing is jagged - and shading is done by mixing a
lighter or darker colour over a shape's own coverage, which is what stops the
soap and the lips reading as flat cut-out paper.

Run: python tools/make_logo.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

DESIGN = 256.0
CENTRE = DESIGN / 2

BG = (25, 28, 36)              # the app's --surface

LIP = (198, 82, 94)            # mid tone of the lips
LIP_DARK = (126, 44, 56)       # where a lip turns away from the light
LIP_LIGHT = (236, 138, 146)    # the wet highlight along the top
MOUTH = (74, 26, 34)           # the inside, just behind the teeth
THROAT = (38, 12, 18)          # deeper in, so the opening has depth
TONGUE = (172, 74, 84)

TOOTH = (246, 247, 250)
TOOTH_SHADE = (196, 202, 214)  # the gum line, and the sides of each tooth

SOAP = (140, 214, 242)         # the top face of the bar
SOAP_SIDE = (86, 168, 208)     # the front face, turned away from the light
SOAP_LIGHT = (222, 246, 255)   # the specular streak
FOAM = (238, 250, 255)
FOAM_SHADE = (176, 214, 232)

SYMBOL = (78, 201, 138)        # the swearing - the app's --accent


# ---------------------------------------------------------------- primitives
def _edge(distance: float) -> float:
    """Anti-aliasing: fade over one unit either side of an edge."""
    return min(1.0, max(0.0, distance + 0.5))


def _mix(base, over, amount: float):
    amount = min(1.0, max(0.0, amount))
    return tuple(base[i] + (over[i] - base[i]) * amount for i in range(3))


def _ellipse(x: float, y: float, cx: float, cy: float, rx: float, ry: float,
             tilt: float = 0.0) -> float:
    """Coverage of an ellipse; the distance is rescaled so the fade stays even."""
    dx, dy = x - cx, y - cy
    if tilt:
        c, s = math.cos(tilt), math.sin(tilt)
        dx, dy = dx * c - dy * s, dx * s + dy * c
    dx, dy = dx / rx, dy / ry
    return _edge((1.0 - math.hypot(dx, dy)) * min(rx, ry))


def _box(x: float, y: float, x0: float, y0: float, x1: float, y1: float,
         radius: float = 0.0) -> float:
    if radius <= 0:
        return _edge(min(x - x0, x1 - x, y - y0, y1 - y))
    cx = max(x0 + radius - x, 0.0, x - (x1 - radius))
    cy = max(y0 + radius - y, 0.0, y - (y1 - radius))
    return _edge(radius - math.hypot(cx, cy))


def _bar(x: float, y: float, x0: float, y0: float, x1: float, y1: float,
         width: float) -> float:
    """A line segment with round ends - what every symbol is built from."""
    vx, vy = x1 - x0, y1 - y0
    span = vx * vx + vy * vy
    t = 0.0 if span == 0 else max(0.0, min(1.0, ((x - x0) * vx + (y - y0) * vy) / span))
    return _edge(width - math.hypot(x - (x0 + t * vx), y - (y0 + t * vy)))


def _arc(x: float, y: float, cx: float, cy: float, radius: float, width: float,
         start: float, end: float) -> float:
    """Part of a ring, for the @ and the two curves of the $."""
    angle = math.atan2(y - cy, x - cx)
    if not start <= angle <= end:
        return 0.0
    return _edge(width - abs(math.hypot(x - cx, y - cy) - radius))


# --------------------------------------------------------------- the drawing
#  The mouth sits low and left, three-quarters open, with the soap wedged in
#  across it. Everything is positioned against these two numbers.
MX, MY = 96.0, 156.0


def tile(x: float, y: float) -> float:
    """Coverage for the background tile, so the corners are not jagged."""
    return _box(x, y, 0, 0, DESIGN, DESIGN, 46)


def opening(x: float, y: float) -> float:
    """The gap between the lips: two arcs meeting at the corners.

    A single ellipse reads as a hole punched in a face. Real lips part along a
    line that is nearly flat at the corners and bows away above and below, so
    the opening is built as the overlap of a wide upper curve and a deeper
    lower one - which is also what gives the bottom lip its fuller shape.
    """
    upper = _ellipse(x, y, MX, MY + 26, 74, 46)     # bowed down from above
    lower = _ellipse(x, y, MX, MY - 16, 74, 52)     # bowed up from below
    return min(upper, lower)


def lips(x: float, y: float) -> tuple[float, float, float]:
    """(body, shadow, highlight) for the lips around that opening."""
    outer = min(_ellipse(x, y, MX, MY + 30, 92, 62),
                _ellipse(x, y, MX, MY - 22, 92, 68))
    body = max(0.0, outer - opening(x, y))
    # The light is up and to the left, so the underside of the lower lip and
    # the inner rim of the upper one fall away into shadow.
    below = _edge(y - (MY + 20)) * body
    rim = max(0.0, opening(x, y + 7) - opening(x, y)) * 0.8
    shadow = max(below * 0.55, rim)
    # A wet streak along the top of the upper lip, and a smaller one below.
    highlight = max(
        _ellipse(x, y, MX - 16, MY - 44, 40, 7, tilt=-0.10) * 0.85,
        _ellipse(x, y, MX + 4, MY + 50, 34, 5, tilt=0.06) * 0.55,
    ) * body
    return body, shadow, highlight


def inside(x: float, y: float) -> tuple[float, float, float, float]:
    """(dark, teeth, tooth shade, tongue) behind the lips."""
    gap = opening(x, y)
    if gap <= 0:
        return 0.0, 0.0, 0.0, 0.0

    # Upper teeth: a row hanging from the top of the opening, each one rounded
    # at its biting edge rather than a rectangle with a scratch down it.
    # The opening runs from about MY-20 to MY+36, so the row hangs inside that
    # - teeth drawn any higher are simply clipped away and the mouth ends up
    # with a white sliver instead of a smile.
    teeth = 0.0
    shade = 0.0
    for offset, half in ((-44, 7), (-29, 8), (-11, 9), (8, 9), (27, 8), (43, 7)):
        tooth = _box(x, y, MX + offset - half, MY - 26,
                     MX + offset + half, MY + 3, 5)
        teeth = max(teeth, tooth)
        # Each tooth is lit from the left, so its right edge darkens - and the
        # whole row is darker where it meets the gum.
        shade = max(shade, tooth * _edge(x - (MX + offset + half - 4)) * 0.45,
                    tooth * _edge((MY - 19) - y) * 0.55)
    teeth = min(teeth, gap)
    shade = min(shade, gap)

    # The tongue, low and half hidden by the soap.
    tongue = min(gap, _ellipse(x, y, MX + 2, MY + 40, 46, 20)) * 0.9
    return gap, teeth, shade, tongue


def soap(x: float, y: float) -> tuple[float, float, float]:
    """(top face, front face, specular) for a bar of soap wedged in the mouth.

    Drawn as two stacked slabs rather than one rectangle: a bar seen slightly
    from above shows its top face and its front, and the join between them is
    what makes it read as an object with thickness instead of a blue label.
    """
    angle = math.radians(-14)
    dx, dy = x - 88.0, y - 180.0
    rx = dx * math.cos(angle) - dy * math.sin(angle)
    ry = dx * math.sin(angle) + dy * math.cos(angle)

    # Sized and set low deliberately: a bar that fills the opening hides the
    # teeth, and then the whole thing stops reading as a mouth at all.
    top = _box(rx, ry, -40, -18, 40, 2, 9)
    front = max(0.0, _box(rx, ry, -38, -6, 38, 17, 8) - top)
    # One streak of reflected light across the top face, plus the softer sheen
    # a wet bar carries near its left edge.
    spec = max(_ellipse(rx, ry, -4, -11, 23, 3, tilt=-0.05),
               _ellipse(rx, ry, -26, -4, 5, 7) * 0.5) * top
    return top, front, spec


def foam(x: float, y: float) -> tuple[float, float]:
    """(bubbles, their shading) drifting off the soap."""
    body = 0.0
    shade = 0.0
    for cx, cy, r in ((28, 116, 13), (52, 94, 8), (150, 128, 9),
                      (170, 106, 5), (16, 150, 7), (72, 78, 5),
                      (40, 196, 6), (124, 172, 7)):
        bubble = _ellipse(x, y, cx, cy, r, r)
        body = max(body, bubble)
        # A bubble is a thin shell: bright at the top-left, darker round the
        # lower right, hollow-looking in between.
        shade = max(shade, bubble * _edge(math.hypot(x - cx + r * .3,
                                                     y - cy + r * .3) - r * .55) * .7)
    return body, shade


def _turn(x: float, y: float, cx: float, cy: float, tilt: float) -> tuple[float, float]:
    """Point (x, y) expressed in a glyph's own rotated, centred coordinates."""
    dx, dy = x - cx, y - cy
    c, s = math.cos(tilt), math.sin(tilt)
    return dx * c - dy * s, dx * s + dy * c


#  The expletive
#  -------------
#  The first version scattered # $ @ ! around the mouth and it read as
#  decoration - four unrelated marks, the visual equivalent of "beep". A swear
#  in a comic is one WORD with its letters swapped for symbols, and the eye
#  reads it as a word: the glyphs sit on a baseline, evenly spaced, same
#  height. So this spells $#!T on a line rising away from the mouth.
#
#  Each glyph is drawn around its own centre in local coordinates, so the whole
#  word can be tilted by changing one number.

def _dollar(lx: float, ly: float, h: float, w: float) -> float:
    """$ - two opposed arcs with the stroke straight through them."""
    r = h * 0.40
    out = _arc(lx, ly, 0, -r * 0.92, r, w, math.radians(-172), math.radians(52))
    out = max(out, _arc(lx, ly, 0, r * 0.92, r, w, math.radians(-52), math.radians(172)))
    return max(out, _bar(lx, ly, 0, -h, 0, h, w * 0.78))


def _hash(lx: float, ly: float, h: float, w: float) -> float:
    """# - uprights leaning the way a real hash does."""
    lean = h * 0.16
    out = 0.0
    for dx in (-h * 0.34, h * 0.34):
        out = max(out, _bar(lx, ly, dx + lean, -h, dx - lean, h, w))
    for dy in (-h * 0.34, h * 0.34):
        out = max(out, _bar(lx, ly, -h * 0.62, dy, h * 0.62, dy, w))
    return out


def _bang(lx: float, ly: float, h: float, w: float) -> float:
    """! - tapering stroke over a dot."""
    out = _bar(lx, ly, 0, -h, 0, h * 0.30, w)
    return max(out, _ellipse(lx, ly, 0, h * 0.78, w * 1.02, w * 1.02))


def _tee(lx: float, ly: float, h: float, w: float) -> float:
    """T - the one real letter, which is what makes the word legible."""
    out = _bar(lx, ly, -h * 0.60, -h + w * 0.5, h * 0.60, -h + w * 0.5, w)
    return max(out, _bar(lx, ly, 0, -h, 0, h, w))


def symbols(x: float, y: float) -> float:
    """$#!T leaving the mouth, on a line rising to the right."""
    tilt = math.radians(-13)          # the word lifts as it travels
    h = 21.0                          # half-height of a glyph
    w = 4.1                           # stroke weight

    # Positions along the baseline, which starts clear of the upper lip.
    glyphs = (
        (_dollar, 112.0, 92.0),
        (_hash, 150.0, 83.0),
        (_bang, 185.0, 75.0),
        (_tee, 214.0, 68.0),
    )

    out = 0.0
    for draw, cx, cy in glyphs:
        lx, ly = _turn(x, y, cx, cy, tilt)
        out = max(out, draw(lx, ly, h, w))

    # One impact tick, past the end of the word. There was a second in front of
    # the $, and sitting up against the glyph it read as an opening quote mark
    # rather than emphasis - the word looked quoted instead of shouted.
    out = max(out, _bar(x, y, 234, 45, 243, 34, w * 0.55))
    return out


def pixel(x: float, y: float):
    """Colour and alpha for one point of the mark, painted back to front."""
    alpha = tile(x, y)
    r, g, b = BG

    gap, teeth, tooth_shade, tongue = inside(x, y)
    if gap > 0:
        # Deeper towards the middle of the opening, so it is a cavity and not
        # a flat maroon shape.
        r, g, b = _mix((r, g, b), MOUTH, gap)
        r, g, b = _mix((r, g, b), THROAT,
                       gap * _ellipse(x, y, MX + 2, MY + 4, 52, 34))
    if tongue > 0:
        r, g, b = _mix((r, g, b), TONGUE, tongue)
    if teeth > 0:
        r, g, b = _mix((r, g, b), TOOTH, teeth)
    if tooth_shade > 0:
        r, g, b = _mix((r, g, b), TOOTH_SHADE, tooth_shade)

    body, shadow, highlight = lips(x, y)
    if body > 0:
        r, g, b = _mix((r, g, b), LIP, body)
    if shadow > 0:
        r, g, b = _mix((r, g, b), LIP_DARK, shadow)
    if highlight > 0:
        r, g, b = _mix((r, g, b), LIP_LIGHT, highlight)

    top, front, spec = soap(x, y)
    if front > 0:
        r, g, b = _mix((r, g, b), SOAP_SIDE, front)
    if top > 0:
        r, g, b = _mix((r, g, b), SOAP, top)
    if spec > 0:
        r, g, b = _mix((r, g, b), SOAP_LIGHT, spec)

    bubbles, bubble_shade = foam(x, y)
    if bubbles > 0:
        r, g, b = _mix((r, g, b), FOAM, bubbles)
    if bubble_shade > 0:
        r, g, b = _mix((r, g, b), FOAM_SHADE, bubble_shade)

    mark = symbols(x, y)
    if mark > 0:
        r, g, b = _mix((r, g, b), SYMBOL, mark)

    return r, g, b, alpha


# ------------------------------------------------------------------- output
def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png(path: Path, size: int, supersample: int = 2, sampler=None) -> None:
    """Render at `supersample`x and average down.

    The shapes already anti-alias themselves, but where three of them meet -
    a tooth against the soap against the dark - one sample per pixel picks a
    winner and the small sizes go crunchy. Averaging four samples fixes it for
    a few seconds of drawing time.
    """
    sampler = sampler or pixel
    scale = size * supersample / DESIGN
    rows = []
    for py in range(size):
        row = bytearray([0])                       # filter byte: none
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(supersample):
                for sx in range(supersample):
                    x = (px * supersample + sx + 0.5) / scale
                    y = (py * supersample + sy + 0.5) / scale
                    for i, value in enumerate(sampler(x, y)):
                        acc[i] += value
            n = supersample * supersample
            row += bytes((int(acc[0] / n), int(acc[1] / n), int(acc[2] / n),
                          int(acc[3] / n * 255)))
        rows.append(bytes(row))

    raw = zlib.compress(b"".join(rows), 9)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + _chunk(b"IHDR", header)
                     + _chunk(b"IDAT", raw)
                     + _chunk(b"IEND", b""))


#  Android crops an installed app's icon to whatever shape the launcher uses -
#  a circle, a squircle, a rounded square - and only the middle 80% of the
#  image is guaranteed to survive. The normal mark runs to the edges, so a
#  circle mask would take the corner of the mouth and the last glyph with it.
#  This draws the same picture at 62% inside a full-bleed background, so the
#  whole thing lands inside the safe zone whatever shape the launcher picks.
MASKABLE_SCALE = 0.62


def _maskable_pixel(x: float, y: float):
    inset = DESIGN * (1 - MASKABLE_SCALE) / 2
    sx = (x - inset) / MASKABLE_SCALE
    sy = (y - inset) / MASKABLE_SCALE
    if 0 <= sx < DESIGN and 0 <= sy < DESIGN:
        r, g, b, alpha = pixel(sx, sy)
        if alpha > 0:
            # The tile's own alpha is composited onto the flat ground, so the
            # rounded corners of the inner drawing disappear into it.
            return (_mix(BG, (r, g, b), alpha) + (1.0,))
    return BG + (1.0,)


def write_all(web: Path) -> None:
    """The favicon, plus the sizes a phone wants for a home-screen icon."""
    for size, name in ((256, "icon.png"), (192, "icon-192.png"),
                       (512, "icon-512.png"), (180, "apple-touch-icon.png")):
        out = web / name
        png(out, size)
        print(f"wrote {out.name} ({size}px, {out.stat().st_size} bytes)")

    for size, name in ((192, "icon-192-maskable.png"),
                       (512, "icon-512-maskable.png")):
        out = web / name
        png(out, size, sampler=_maskable_pixel)
        print(f"wrote {out.name} ({size}px, safe-zone, "
              f"{out.stat().st_size} bytes)")


if __name__ == "__main__":
    write_all(Path(__file__).resolve().parents[1] / "web")
