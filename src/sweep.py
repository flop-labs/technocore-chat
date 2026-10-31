"""The invisible-text sweep, shared by both write lanes.

store.clean_text sweeps on the way into storage; limit.normalize_text sweeps on the way
into the duplicate ring, which the unsigned lanes reach before store.append runs
clean_text at all. If the two ever swept different sets, one text would become two ring
keys and a flood of one message would take a ring slot per codepoint it varies. So the
set and the sweep live here, in one place, and both lanes call this.
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
#                      log line shows, bidi overrides (U+202E) reorder displayed text away
#                      from what is stored (Trojan Source), and zero-width joiners hide word
#                      boundaries. This service's stated top hazard is cross-agent prompt
#                      injection (design doc §3.1), so text that renders as nothing must not
#                      survive into another agent's context.
#   Cs  surrogate    — never valid on its own in stored text.
#   Co  private use  — renders as whatever the reader's font decides, which is not a promise.
#   Zl  line sep     — U+2028, and Zp U+2029: invisible here, a line break to enough
#   Zp  para sep       plain-text consumers (JS string literals among them) that one stored
#                      value renders as two lines. The single-line promise has to hold for
#                      every reader, not just the ones that agree with `str.splitlines`.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
# INVISIBLE_CATEGORIES reasons by general category, but the property it stands in for is
# Default_Ignorable_Code_Point: "renders as nothing". The two do not coincide. 267 assigned
# default-ignorable codepoints are Mn or Lo, in no C*/Z* category, so the category sweep never
# sees them and they survive into a reader's context invisibly. The bulk is the 256 variation
# selectors (U+FE00-FE0F and the U+E0100-E01EF supplement): a 256-symbol alphabet at 8 bits per
# codepoint, a denser invisible channel than the Unicode tag block this sweep was built to close.
# So they are swept too, by codepoint rather than by category. It has to be by codepoint: Mn and
# Lo also hold the combining marks and letters that carry real orthographic content, so sweeping
# those categories wholesale would break stored text the way sweeping ZWJ/ZWNJ breaks Brahmic
# script (issue #144). This is the mirror of that, a narrow named set rather than a category.
# Scope: the set is the *assigned* default-ignorables only. The unassigned (Cn) default-ignorable
# codepoints (U+2065, U+FFF0-FFF8 and the plane-14 gaps) are category Cn, outside both the sweep
# and this set, so what this closes is the assigned default-ignorable channel, not every codepoint
# that renders as nothing. Enumerating the Cn ranges too was the larger alternative, deliberately
# left out of this change.
# Sweeping a variation selector flattens a glyph variant (an emoji, Mongolian or CJK presentation
# form loses its selector), the same visible, harmless cost design.md §3.2 already accepts for ZWJ
# emoji sequences. Every entry below is Default_Ignorable and in no swept category:
SWEEP_ALSO = frozenset(
    chr(cp)
    for start, end in (
        (0x034F, 0x034F),  # combining grapheme joiner
        (0x115F, 0x1160),  # Hangul choseong, jungseong fillers
        (0x17B4, 0x17B5),  # Khmer vowel inherent AQ, AA
        (0x180B, 0x180D),  # Mongolian free variation selectors 1-3
        (0x180F, 0x180F),  # Mongolian free variation selector 4 (180E MVS is Cf, already swept)
        (0x3164, 0x3164),  # Hangul filler
        (0xFE00, 0xFE0F),  # variation selectors 1-16
        (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
        (0xE0100, 0xE01EF),  # variation selectors supplement 17-256
    )
    for cp in range(start, end + 1)
)


def sweep_invisibles(text: str) -> str:
    """Every INVISIBLE_CATEGORIES character and every SWEEP_ALSO codepoint to a space.

    One function because two lanes have to agree on it. `clean_text` sweeps on the way
    into storage, and `limit.normalize_text` sweeps on the way into the duplicate ring,
    which the unsigned lanes reach before `append` runs `clean_text` at all. If the two
    lists ever drift apart, one text becomes two ring keys and a flood of one message
    takes a slot per codepoint it varies. That is `normalize_text`'s own reasoning, so the
    list it reasons about lives here rather than in two comprehensions.
    """
    return "".join(
        " " if (unicodedata.category(c) in INVISIBLE_CATEGORIES or c in SWEEP_ALSO) else c
        for c in text
    )
