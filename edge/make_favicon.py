#!/usr/bin/env python3
"""Draw edge/assets/favicon.ico from the Technocore mark. Run when the mark changes.

    uv run --with pillow edge/make_favicon.py

A committed binary nobody can regenerate is a binary nobody can change, so the icon is built
by this script and this file is the only place its treatment is described. Deliberately not
part of deploy.sh: the output is tracked, and a deploy that could silently redraw the mark is
a deploy that can ship a different one by accident.

The mark is `docs/brand/technocore_Icon_Accent.svg`: the studio's vector, whose path data is
byte-identical to flop-core's `docs/brand/technocore/svg/` copy (docs/brand/README.md says
how to check). It is one closed path of straight lines and cubic curves, and this script
rasterises that path itself — the curves are flattened to a polygon, the polygon is filled at
SUPERSAMPLE times the final size, and the frame is reduced with LANCZOS. That is the whole
renderer. It means the icon is drawn from the same bytes flop-core checksums rather than from
a raster export of them: the delivered PNGs carry ~0.85% more ink than the geometry (an
export artifact, per the brand README), and a 16 px favicon is exactly where a fraction of a
pixel of ink shows. The fill is read from the file, not restated here, so nothing here
recolours anything.

TILE is the one judgement call. The mark ships on transparency, and #00B4D8 against a white
tab bar is about 2.3:1 — legible, but the weakest thing in the row. Compositing it on the
page's own base colour makes the icon read the same on a light tab bar, a dark one, a
bookmark list and a phone home screen. That is brand furniture the mark did not come with,
so it is one constant rather than a hundred lines: set it False to ship the bare mark. The
tile is Base, and Accent on Base is an approved plated combination in the brand README.

PAD frames the mark at 76% of the box, the framing the brand guidelines give for a square
icon slot (BRAND_GUIDELINES.md §6). The mark is a 1.57:1 landscape shape, so it fills the
box in width and stands about half its height; the guidelines accept that rather than
asking for a redraw, and so does this.

Sizes are 16/32/48. 16 is what a browser tab renders and the only one worth arguing about;
the mark is three nodes and two links, which survives it.
"""

from __future__ import annotations

import pathlib
import re

# Pillow is deliberately not a project dependency: this runs when the mark changes, which is
# rarely and by hand, and neither the service nor deploy.sh imports it. `uv run --with pillow`
# is the whole install story.
from PIL import Image, ImageDraw  # ty: ignore[unresolved-import]

BASE = "#0A1128"  # --base in src/humans.html; the tile, which is the one thing drawn here
TILE = True  # see the module docstring
PAD = 0.12 if TILE else 0.03  # 76% of the box on a tile; a bare mark can run nearer the edge
SUPERSAMPLE = 16  # composited this much larger, then LANCZOS down, so edges stay clean
CURVE_STEPS = 16  # line segments per cubic; at SUPERSAMPLE x 48 px that is sub-pixel
SIZES = (16, 32, 48)
HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE.parent / "docs" / "brand" / "technocore_Icon_Accent.svg"
OUT = HERE / "assets" / "favicon.ico"

_PATH = re.compile(r'<path id="icon" fill="(#[0-9A-Fa-f]{6})" d="([^"]+)"')
_TOKEN = re.compile(r"[A-Za-z]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def outline(d: str) -> list[tuple[float, float]]:
    """The path flattened to one polygon, in the SVG's own units.

    Absolute M, L, C and Z, one subpath: that is what the brand file contains, and a mark
    that arrives drawn any other way should fail here, loudly, rather than render a guess.
    """
    tokens = _TOKEN.findall(d)
    pts: list[tuple[float, float]] = []
    i = 0

    def take(n: int) -> tuple[float, ...]:
        nonlocal i
        vals = tuple(float(t) for t in tokens[i : i + n])
        if len(vals) != n:
            raise ValueError("path ends mid-command")
        i += n
        return vals

    while i < len(tokens):
        cmd = tokens[i]
        i += 1
        if cmd == "M":
            if pts:
                raise ValueError("more than one subpath")
            x, y = take(2)
            pts.append((x, y))
        elif cmd == "L":
            x, y = take(2)
            pts.append((x, y))
        elif cmd == "C":
            x1, y1, x2, y2, x3, y3 = take(6)
            x0, y0 = pts[-1]
            for k in range(1, CURVE_STEPS + 1):
                t = k / CURVE_STEPS
                u = 1 - t
                pts.append(
                    (
                        u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                        u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
                    )
                )
        elif cmd == "Z":
            if i != len(tokens):
                raise ValueError("Z before the end of the path")
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
    return pts


def load(path: pathlib.Path) -> tuple[list[tuple[float, float]], str]:
    """The mark's outline and its fill, both taken from the file and nowhere else."""
    found = _PATH.search(path.read_text(encoding="utf-8"))
    if not found:
        raise SystemExit(f'{path}: no <path id="icon" fill="…" d="…">')
    return outline(found.group(2)), found.group(1)


def draw(mark: list[tuple[float, float]], fill: str, size: int) -> Image.Image:
    """One frame. `size` is the final edge in pixels; the mark is fitted by width.

    Fitted by width and centred vertically because the mark is wider than it is tall, so
    fitting by height would push it off both sides.
    """
    r = size * SUPERSAMPLE
    im = Image.new("RGBA", (r, r), (0, 0, 0, 0))
    if TILE:
        ImageDraw.Draw(im).rounded_rectangle([0, 0, r - 1, r - 1], radius=int(r * 0.20), fill=BASE)
    xs, ys = zip(*mark, strict=True)
    x0, y0, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    scale = r * (1 - 2 * PAD) / w
    ox = (r - w * scale) / 2 - x0 * scale
    oy = (r - h * scale) / 2 - y0 * scale
    ImageDraw.Draw(im).polygon([(x * scale + ox, y * scale + oy) for x, y in mark], fill=fill)
    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    mark, fill = load(SOURCE)
    # Largest first: Pillow saves from the base image and drops any requested size larger
    # than it, so a 16px base silently yields a single-frame icon. It matches append_images
    # by size, which is what keeps each frame the one drawn for it rather than a resize.
    order = sorted(SIZES, reverse=True)
    frames = [draw(mark, fill, s) for s in order]
    frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in order], append_images=frames[1:])
    print(f"{OUT} ({OUT.stat().st_size} bytes, sizes {list(order)}, tile={TILE}, fill={fill})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
