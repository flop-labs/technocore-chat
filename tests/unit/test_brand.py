"""docs/brand/ is a copy of the studio artwork, and a copy is only worth tracking if it can
be shown to still be one.

The source of truth is flop-core's `docs/brand/technocore/svg/`, checksummed there in
SHA256SUMS. The files here differ from those in exactly one way: the root <svg> element's
viewBox is cropped to the artwork, because the delivered canvas is 2048 px square with the
lockup occupying 8.7% of its height, and an <img> of that is a page of padding with a logo
somewhere in it. The brand guidelines allow precisely that edit — "keep the path data and
fill byte-identical; change only viewBox" (§6) — so the check is that everything after the
root tag hashes to what the source hashes to. A redraw, a re-export, an optimiser pass or a
recolour all fail here; a re-crop does not.

The digests are of the content inside the root element of the flop-core files at commit
7a3d9a3 (docs(brand): add Technocore logo assets and usage guidelines, #1484). To refresh a
file, copy it from there and re-crop; to refresh a digest, say which flop-core commit in the
PR body — a digest changed without one is the drift this exists to catch.
"""

import hashlib
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MUTANT_UNDER_TEST") is not None,
    reason="reads docs/brand/ artwork, not src/; a mutmut copy carries no docs/ so this would "
    "measure the copy, not a mutant",
)

BRAND = Path(__file__).resolve().parents[2] / "docs" / "brand"
ROOT_TAG = re.compile(r'^<svg xmlns="http://www.w3.org/2000/svg" viewBox="([^"]+)">')

# sha256 of the bytes after the root <svg …> tag, i.e. of every path and fill.
SOURCE = {
    "technocore_Icon_Accent.svg": "f966cde649290b0e274c498b1a3cdd79d7107110371405feda7ccffec609459a",
    "technocore_Primary_Lockup_Base_Icon_Accent.svg": (
        "70a8b2223cdae75feb12c111a8336d519810eeee6bca2060ba4ff8a8fd61bec4"
    ),
    "technocore_Reverse_Lockup_IceWhite_Icon_Accent.svg": (
        "9ce6b9323c7a71594f55f5f44fe26bbd7be5b80dec0eea4cb5b07882997b2a9d"
    ),
}

# The palette, inherited from FLOP with no additions: the only three values a fill may be.
PALETTE = {"#00B4D8", "#0A1128", "#F5F7FA"}


def _split(name: str) -> tuple[str, str]:
    text = (BRAND / name).read_text(encoding="utf-8")
    root = ROOT_TAG.match(text)
    assert root, f"{name}: the root element is not the one bare <svg viewBox> this repo tracks"
    return root.group(1), text[root.end() :]


@pytest.mark.parametrize("name", sorted(SOURCE))
def test_the_artwork_is_the_studio_artwork(name):
    _, inner = _split(name)
    assert hashlib.sha256(inner.encode("utf-8")).hexdigest() == SOURCE[name], (
        f"{name}: the path data differs from flop-core's — copy the file from there and "
        "re-crop the viewBox; never redraw, optimise or recolour it here"
    )


@pytest.mark.parametrize("name", sorted(SOURCE))
def test_the_viewbox_is_cropped_to_the_artwork_not_the_canvas(name):
    """The one edit the guidelines allow, and the reason the copies are here at all."""
    box, _ = _split(name)
    x, y, w, h = (float(v) for v in box.split())
    assert (w, h) != (2048.0, 2048.0), f"{name}: still the delivered canvas, not the artwork"
    assert 0 < w and 0 < h, box
    # A crop is a window onto the 2048 canvas, not a translation of the artwork.
    assert 0 <= x and 0 <= y and x + w <= 2048 and y + h <= 2048, box


@pytest.mark.parametrize("name", sorted(SOURCE))
def test_every_fill_is_a_palette_value(name):
    _, inner = _split(name)
    fills = set(re.findall(r'fill="([^"]+)"', inner))
    assert fills and fills <= PALETTE, f"{name}: fills {fills - PALETTE} are not in the palette"


def test_nothing_else_is_tracked_as_brand_artwork():
    """Every file here is pinned above, or it is an unverified copy nobody can vouch for."""
    tracked = {p.name for p in BRAND.glob("*.svg")}
    assert tracked == set(SOURCE), tracked ^ set(SOURCE)


def test_the_page_inlines_the_reverse_lockup_unchanged():
    """/humans carries the lockup as markup rather than fetching it, so the page has its own
    copy of the path data — which is the same drift risk as any other copy. It must match
    the tracked file, path for path, and it must be the reverse lockup: the page's ground is
    Base, and the reverse (Ice White word mark, Accent icon) is the one approved there.
    """
    box, inner = _split("technocore_Reverse_Lockup_IceWhite_Icon_Accent.svg")
    page = (BRAND.parents[1] / "src" / "humans.html").read_text(encoding="utf-8")
    masthead = re.search(r'<svg class="lockup"[^>]*>(.*?)</svg>', page, re.DOTALL)
    assert masthead, "no masthead lockup in /humans"
    assert f'viewBox="{box}"' in masthead.group(0), "the masthead crop differs from the file's"
    want = re.findall(r'(fill="#[0-9A-F]{6}" d="[^"]+")', inner)
    got = re.findall(r'(fill="#[0-9A-F]{6}" d="[^"]+")', masthead.group(1))
    assert got == want, "the inline lockup has drifted from docs/brand/ — re-copy the paths"
