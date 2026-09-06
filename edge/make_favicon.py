#!/usr/bin/env python3
"""Draw edge/assets/favicon.ico from the Technocore mark. Run when the mark changes.

    uv run --with pillow edge/make_favicon.py

A committed binary nobody can regenerate is a binary nobody can change, so the icon is built
by this script and this file is the only place its treatment is described. Deliberately not
part of deploy.sh: the output is tracked, and a deploy that could silently redraw the mark is
a deploy that can ship a different one by accident.

`assets/icon-source.png` is the mark itself, trimmed to its bounding box and reduced to 512
wide. It is derived from `brand/Technocore/Technocore Files/Svg/Icon 1.svg` in the main
checkout — which is a 2048px PNG inside an SVG wrapper, and is not tracked here, so a copy
has to live in the repo for this to be reproducible. Its fill is #00B4D8, the same value
src/humans.html calls `--accent`, so nothing here recolours anything.

TILE is the one judgement call. The mark ships on transparency, and #00B4D8 against a white
tab bar is about 2.3:1 — legible, but the weakest thing in the row. Compositing it on the
page's own base colour makes the icon read the same on a light tab bar, a dark one, a
bookmark list and a phone home screen. That is brand furniture the mark did not come with,
so it is one constant rather than a hundred lines: set it False to ship the bare mark.

Sizes are 16/32/48. 16 is what a browser tab renders and the only one worth arguing about;
the mark is three nodes and two links, which survives it.
"""

from __future__ import annotations

import pathlib

# Pillow is deliberately not a project dependency: this runs when the mark changes, which is
# rarely and by hand, and neither the service nor deploy.sh imports it. `uv run --with pillow`
# is the whole install story.
from PIL import Image, ImageDraw  # ty: ignore[unresolved-import]

BASE = "#0A1128"  # --base in src/humans.html
TILE = True  # see the module docstring
PAD = 0.14 if TILE else 0.03  # a bare mark can run closer to the edge; a tile needs a margin
SUPERSAMPLE = 8  # composited this much larger, then LANCZOS down, so edges stay clean
SIZES = (16, 32, 48)
HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE / "assets" / "icon-source.png"
OUT = HERE / "assets" / "favicon.ico"


def draw(mark: Image.Image, size: int) -> Image.Image:
    """One frame. `size` is the final edge in pixels; the mark is fitted by width.

    Fitted by width and centred vertically because the mark is wider than it is tall, so
    fitting by height would push it off both sides.
    """
    r = size * SUPERSAMPLE
    im = Image.new("RGBA", (r, r), (0, 0, 0, 0))
    if TILE:
        ImageDraw.Draw(im).rounded_rectangle([0, 0, r - 1, r - 1], radius=int(r * 0.20), fill=BASE)
    w = int(r * (1 - 2 * PAD))
    h = max(1, round(mark.height * w / mark.width))
    im.alpha_composite(mark.resize((w, h), Image.LANCZOS), ((r - w) // 2, (r - h) // 2))
    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    mark = Image.open(SOURCE).convert("RGBA")
    mark = mark.crop(mark.getbbox())  # the source is already trimmed; this keeps it true
    # Largest first: Pillow saves from the base image and drops any requested size larger
    # than it, so a 16px base silently yields a single-frame icon. It matches append_images
    # by size, which is what keeps each frame the one drawn for it rather than a resize.
    order = sorted(SIZES, reverse=True)
    frames = [draw(mark, s) for s in order]
    frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in order], append_images=frames[1:])
    print(f"{OUT} ({OUT.stat().st_size} bytes, sizes {list(order)}, tile={TILE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
