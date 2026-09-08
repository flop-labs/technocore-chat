# Brand artwork used by this service

**Source of truth:** [`flop-core/docs/brand/technocore`](https://github.com/flop-labs/flop-core/tree/main/docs/brand/technocore)
— its `README.md` is the file map and `BRAND_GUIDELINES.md` the usage rules (clear space,
minimum size, app-icon framing, misuse). Nothing about the mark is decided here; this
directory holds copies of the three files this repo places, and says where each one goes.

Copied from flop-core at `7a3d9a3` (*docs(brand): add Technocore logo assets and usage
guidelines*, #1484).

| File | What | Used |
|---|---|---|
| `technocore_Icon_Accent.svg` | The mark, Accent on transparent | `edge/make_favicon.py` draws `/favicon.ico` from it |
| `technocore_Reverse_Lockup_IceWhite_Icon_Accent.svg` | Ice White word mark, Accent icon — **on Base only** | Inlined as the `/humans` masthead; the root `README.md` in dark mode |
| `technocore_Primary_Lockup_Base_Icon_Accent.svg` | Base word mark, Accent icon — on Ice White or paper | The root `README.md` in light mode |

## The one edit

The delivered files sit on a 2048 px square canvas with the lockup at 8.7% of its height.
Placed as an `<img>` that is a page of padding with a logo somewhere in it, so the root
element's `viewBox` here is cropped to the artwork's bounding box, measured off the path
data — the guidelines' own instruction for placement (§6: *"keep the path data and fill
byte-identical; change only viewBox"*). Everything after the root tag is the studio file,
byte for byte:

```bash
for f in docs/brand/*.svg; do
  diff <(sed '1s/^<svg[^>]*>//' "$f") \
       <(sed '1s/^<svg[^>]*>//' "$FLOP_CORE/docs/brand/technocore/svg/$(basename "$f")") \
    && echo "$(basename "$f"): identical"
done
```

`tests/unit/test_brand.py` pins the same thing without needing the other checkout: a
sha256 of the content inside the root element, taken from the flop-core files at the
commit above. Refreshing a file means copying it from flop-core and re-cropping; refreshing
a digest means naming the flop-core commit in the PR body.

Clear space is *not* baked into the crop. The guidelines want half the icon's height on
every side of the artwork, measured from the artwork; `/humans` provides it in CSS (see
`.lockup` in `src/humans.html`) and the README through the page's own margins.

## Where the rules bend, and why

- **The README on GitHub's dark theme is not Base.** The reverse lockup is approved on Base
  (`#0A1128`) only, and GitHub paints `#0d1117`. Both are the same deep navy-black to the
  eye and the alternative — the one-colour reversed lockup — gives up the Accent icon for a
  difference nobody can see. Judged the lesser deviation; flag it if the studio disagrees.
- **The favicon is plated.** The mark ships on transparency, and `make_favicon.py` sets it
  on a rounded Base tile at 76% of the box, the framing §6 gives for a square icon slot.
  Accent on Base is an approved plated combination; the rounding is furniture the mark did
  not come with, and that file says why it is there and how to turn it off.
