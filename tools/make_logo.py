"""Draw the Cleanarr mark: a mouth with a bar of soap in it, and the swear it
was about to say in a speech bubble.

The drawing is one SVG, built here and written to web/logo.svg - which is also
the favicon, so it is sharp at any size. The PNGs a phone or Unraid asks for
are rendered from that same SVG by Chromium, through Playwright (the UI tests
need it anyway):

    pip install playwright && python -m playwright install chromium
    python tools/make_logo.py

The swear is spelled $#!T rather than scattered as # $ @ ! - a comic's grawlix
is read as one word when the glyphs share a baseline and a height, and the one
real letter at the end is what makes it legible. The glyphs are strokes, not
text, so the SVG looks the same whatever fonts a browser has.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

# Colours. The bubble is the app's accent green; the ground is its dark surface.
BG_TOP, BG_BOTTOM = "#232a38", "#11141b"
INK = "#0e1a14"

# The opening between the lips, used for the mouth and to clip what is in it.
INNER = ("M52,150 C66,132 98,126 118,132 C140,126 172,132 186,150 "
         "C172,190 146,204 119,204 C92,204 66,190 52,150 Z")
OUTER = ("M30,150 C46,110 86,100 118,114 C150,100 190,110 208,150 "
         "C194,206 152,228 119,228 C86,228 44,206 30,150 Z")

# (x, y, radius) of the bubbles drifting off the soap.
FOAM = [(54, 104, 15), (33, 80, 8), (60, 66, 5), (214, 196, 11), (226, 170, 6),
        (38, 196, 7)]
# Suds on the end of the bar, where it sticks out of the mouth.
SUDS = [(206, 150, 6), (216, 142, 4.5), (198, 143, 3.5), (214, 156, 3)]

# Android crops an installed icon to the launcher's shape and keeps only a
# circle 80% across, so the maskable versions draw the mark smaller on a
# full-bleed ground. The speech bubble's far corner is the furthest point from
# the centre, at about 0.59 of the width; 0.66 of that lands inside the circle.
MASKABLE_SCALE = 0.66


def _swear(x: float, y: float, gap: float) -> tuple[str, tuple[float, float]]:
    """Stroke paths for $#!T starting at x on the line y, and where the ! dot goes."""
    paths = [
        # $ - an S with the stroke straight through it
        f"M{x + 8},{y - 10} C{x + 5},{y - 15} {x - 9},{y - 16} {x - 9},{y - 7} "
        f"C{x - 9},{y} {x + 9},{y - 1} {x + 9},{y + 7} "
        f"C{x + 9},{y + 16} {x - 6},{y + 16} {x - 9},{y + 10} "
        f"M{x},{y - 19} L{x},{y + 19}",
    ]
    x += gap
    # # - uprights leaning the way a real hash does
    paths.append(f"M{x - 3},{y - 15} L{x - 7},{y + 15} M{x + 7},{y - 15} L{x + 3},{y + 15} "
                 f"M{x - 11},{y - 5} L{x + 11},{y - 5} M{x - 12},{y + 5} L{x + 10},{y + 5}")
    x += gap * 0.8
    bang = (x, y + 14)
    paths.append(f"M{x},{y - 16} L{x},{y + 4}")
    x += gap * 0.8
    paths.append(f"M{x - 10},{y - 14} L{x + 10},{y - 14} M{x},{y - 14} L{x},{y + 16}")
    return " ".join(paths), bang


def _bubble(x: float, y: float, r: float) -> str:
    """A soap bubble: nearly clear, a bright rim, and a highlight top left."""
    rim = max(1.6, r * 0.17)
    arc = r * 0.55
    return (f'<circle cx="{x}" cy="{y}" r="{r}" fill="url(#foam)" stroke="#ffffff" '
            f'stroke-opacity=".9" stroke-width="{rim:.1f}"/>'
            f'<path d="M{x - arc:.1f},{y - r * 0.15:.1f} A{arc:.1f},{arc:.1f} 0 0 1 '
            f'{x - r * 0.1:.1f},{y - arc - r * 0.05:.1f}" stroke="#ffffff" '
            f'stroke-width="{rim:.1f}" stroke-linecap="round" fill="none"/>')


def svg(size: int | None = None, rounded: bool = True, scale: float = 1.0) -> str:
    """The mark. `rounded` gives the tile rounded corners; `scale` shrinks the
    drawing inside a full-bleed ground, for the maskable icons."""
    swear, (bang_x, bang_y) = _swear(140, 60, 25)
    inset = 256 * (1 - scale) / 2
    dims = f' width="{size}" height="{size}"' if size else ""
    foam = "".join(_bubble(*b) for b in FOAM)
    suds = "".join(f'<circle cx="{x}" cy="{y}" r="{r}" opacity="{0.95 - i * 0.06:.2f}"/>'
                   for i, (x, y, r) in enumerate(SUDS))
    teeth = "".join(f'<rect x="{x}" y="112" width="{w}" height="{h}" rx="7"/>'
                    for x, w, h in ((50, 24, 38), (76, 21, 41), (99, 20, 43),
                                    (121, 20, 43), (143, 21, 41), (166, 24, 38)))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"{dims}>
<title>Cleanarr</title>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{BG_TOP}"/><stop offset="1" stop-color="{BG_BOTTOM}"/></linearGradient>
  <radialGradient id="glow" cx=".45" cy=".62" r=".5"><stop offset="0" stop-color="#4ec98a" stop-opacity=".22"/><stop offset="1" stop-color="#4ec98a" stop-opacity="0"/></radialGradient>
  <linearGradient id="lip" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f06f7e"/><stop offset=".55" stop-color="#d2475a"/><stop offset="1" stop-color="#9e2a3d"/></linearGradient>
  <radialGradient id="cavity" cx=".5" cy=".45" r=".6"><stop offset="0" stop-color="#2a0710"/><stop offset="1" stop-color="#5a1424"/></radialGradient>
  <linearGradient id="tooth" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#dfe5ee"/><stop offset=".35" stop-color="#ffffff"/><stop offset="1" stop-color="#e8edf4"/></linearGradient>
  <linearGradient id="soapTop" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c9f3ff"/><stop offset="1" stop-color="#7fd6f5"/></linearGradient>
  <linearGradient id="soapSide" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5fc0e6"/><stop offset="1" stop-color="#3a98c6"/></linearGradient>
  <linearGradient id="speech" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6ee0a4"/><stop offset="1" stop-color="#34b374"/></linearGradient>
  <radialGradient id="foam" cx=".4" cy=".35" r=".7"><stop offset="0" stop-color="#ffffff" stop-opacity=".04"/><stop offset=".75" stop-color="#bff0ff" stop-opacity=".16"/><stop offset="1" stop-color="#e8fbff" stop-opacity=".5"/></radialGradient>
  <clipPath id="inner"><path d="{INNER}"/></clipPath>
</defs>
<rect width="256" height="256" rx="{56 if rounded else 0}" fill="url(#bg)"/>
<g transform="translate({inset:g},{inset:g}) scale({scale:g})">
  <rect width="256" height="256" fill="url(#glow)"/>
  <path d="{INNER}" fill="url(#cavity)"/>
  <g clip-path="url(#inner)">
    <ellipse cx="120" cy="206" rx="46" ry="22" fill="#c85466"/>
    <g fill="url(#tooth)">{teeth}</g>
  </g>
  <path fill-rule="evenodd" fill="url(#lip)" d="{OUTER} {INNER}"/>
  <path d="M58,124 C78,112 100,111 114,119" stroke="#ffc2c9" stroke-width="5" stroke-linecap="round" fill="none" opacity=".75"/>
  <path d="M92,219 C108,224 132,224 148,219" stroke="#ff9aa8" stroke-width="4" stroke-linecap="round" fill="none" opacity=".55"/>
  <g transform="rotate(-11 136 176)">
    <rect x="84" y="166" width="136" height="36" rx="12" fill="url(#soapSide)"/>
    <rect x="84" y="158" width="136" height="27" rx="12" fill="url(#soapTop)"/>
    <rect x="116" y="164" width="72" height="15" rx="7.5" fill="none" stroke="#ffffff" stroke-opacity=".75" stroke-width="2.4"/>
    <path d="M96,164 C110,161 160,161 180,163" stroke="#ffffff" stroke-width="3.2" stroke-linecap="round" opacity=".85" fill="none"/>
  </g>
  <g fill="#ffffff">{suds}</g>
  {foam}
  <g transform="rotate(-7 180 58)">
    <path d="M136,26 H222 A18,18 0 0 1 240,44 V76 A18,18 0 0 1 222,94 H160 L138,114 L144,94 H136 A18,18 0 0 1 118,76 V44 A18,18 0 0 1 136,26 Z" fill="url(#speech)"/>
    <path d="{swear}" stroke="{INK}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    <circle cx="{bang_x}" cy="{bang_y}" r="4.2" fill="{INK}"/>
  </g>
</g>
</svg>
'''


def render(page, markup: str, size: int, out: Path) -> None:
    """One PNG of `markup` at `size` pixels, with transparent corners kept."""
    data = base64.b64encode(markup.encode("utf8")).decode()
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(f'<body style="margin:0"><img id="m" width="{size}" height="{size}" '
                     f'src="data:image/svg+xml;base64,{data}"></body>')
    page.wait_for_function("document.getElementById('m').complete")
    page.locator("#m").screenshot(path=str(out), omit_background=True)
    print(f"wrote {out.relative_to(ROOT)} ({size}px, {out.stat().st_size} bytes)")


def write_all() -> None:
    from playwright.sync_api import sync_playwright

    (WEB / "logo.svg").write_text(svg(), encoding="utf8")
    print("wrote web/logo.svg")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for size, name in ((256, "icon.png"), (192, "icon-192.png"), (512, "icon-512.png")):
            render(page, svg(size), size, WEB / name)
        # iOS rounds the corners itself and fills transparency with black, so
        # its icon is the full square.
        render(page, svg(180, rounded=False), 180, WEB / "apple-touch-icon.png")
        for size, name in ((192, "icon-192-maskable.png"), (512, "icon-512-maskable.png")):
            render(page, svg(size, rounded=False, scale=MASKABLE_SCALE), size, WEB / name)
        browser.close()


if __name__ == "__main__":
    write_all()
