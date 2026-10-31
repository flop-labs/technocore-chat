"""The invisible-text sweep for clean_text.

Kept here, not inline in store.clean_text, so store.py stays within its size budget and the
category list plus the two exempt joiners have one documented home. INVISIBLE_CATEGORIES is
the render-as-nothing set; SWEEP_EXEMPT holds the two format characters clean_text keeps.
"""

from __future__ import annotations

import unicodedata

# The Unicode categories `clean_text` replaces with a space, and why each is on the list.
# One list, in one place: the reason a value is swept is the reason it is named here, and a
# docstring that also enumerated them would be a second copy to keep in step.
#
#   Cc  control      — C0/C1 would break the JSONL one-record-per-line invariant.
#   Cf  format       — the *invisible instruction* smuggling vector against LLM readers.
#                      Unicode tag characters U+E0000–U+E007F encode ASCII that no human or
#                      log line shows, and bidi overrides (U+202E) reorder displayed text
#                      away from what is stored (Trojan Source). This service's stated top
#                      hazard is cross-agent prompt injection (design doc §3.1), so text that
#                      renders as nothing must not survive into another agent's context. Two
#                      Cf characters are held out by SWEEP_EXEMPT below: their channel is a
#                      fraction of the tag block's and they spell real words, so the rule
#                      that takes the rest would corrupt them — see there for the measurement.
#   Cs  surrogate    — never valid on its own in stored text.
#   Co  private use  — renders as whatever the reader's font decides, which is not a promise.
#   Zl  line sep     — U+2028, and Zp U+2029: invisible here, a line break to enough
#   Zp  para sep       plain-text consumers (JS string literals among them) that one stored
#                      value renders as two lines. The single-line promise has to hold for
#                      every reader, not just the ones that agree with `str.splitlines`.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")

# The two format characters the sweep holds out: U+200C ZERO WIDTH NON-JOINER and U+200D
# ZERO WIDTH JOINER. Both are Cf, so INVISIBLE_CATEGORIES would otherwise take them, but the
# reasoning that lists Cf carves them out rather than covering them. Two properties set them
# apart from every other Cf character:
#   1. Their channel is a fraction of the one the sweep exists to close, measured rather
#      than assumed. The tag block carries 7 bits per codepoint and can be inserted almost
#      anywhere, so 0.73% density hides arbitrary ASCII. A joiner carries at most 1 bit and
#      only between two characters a reader already sees, so the same payload needs about
#      5% density and shows up as visibly broken spelling. Not zero, and this comment does
#      not claim zero: small enough that exempting them does not reopen the §3.1 hazard the
#      rest of the list closes.
#   2. They are orthographic, not decorative. Every Brahmic script (Devanagari, Bengali,
#      Tamil, Telugu, and a dozen more) spells conjuncts with ZWJ and blocks them with ZWNJ,
#      and Persian/Urdu use ZWNJ inside a word. Sweeping them silently rewrites the spelling
#      of a correctly formed word for ~1B readers, and because a signed write is verified
#      against the *swept* text, a signature over that word then 403s with no hint why.
# Neither is a line break, so the one-record-per-line invariant the sweep exists to keep is
# untouched. Every other Cf character — tag block, bidi overrides, ZWSP, word joiner, BOM —
# still goes.
SWEEP_EXEMPT = frozenset("\u200c\u200d")  # U+200C ZWNJ, U+200D ZWJ


def sweep_invisibles(text: str) -> str:
    """Every INVISIBLE_CATEGORIES character to a space, except the two SWEEP_EXEMPT joiners.

    A signed write is verified against this swept text, so keeping U+200C/U+200D here means an
    Indic word or a ZWJ emoji sequence round-trips and still verifies. Every other invisible
    still goes.
    """
    return "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES and c not in SWEEP_EXEMPT else c
        for c in text
    )


def nothing_visible(swept: str) -> bool:
    """True when swept text has nothing a reader can see: empty, or only joiners and spaces.

    The exemption keeps the joiners because they spell the visible characters beside them.
    With nothing beside them they spell nothing, and a text made only of them renders as
    blank, which the sweep exists to refuse. Before the exemption the sweep turned such a
    text into spaces and the empty check caught it, so keeping that refusal is what stops
    the exemption from opening a blank-message lane that main does not have.
    """
    return not "".join(c for c in swept if c not in SWEEP_EXEMPT).strip()
