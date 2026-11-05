"""Run: uv run --group dev python -m pytest tests

DIGIT in the HTTP qvalue grammar is ASCII (RFC 5234 §B.1). Python's `\\d` is not: it also
matches other Unicode decimal digits, and `float()` reads those as numbers, so the two
compose into a q the grammar does not allow.

This sits at the unit level rather than in test_docs.py on purpose. Header values decode
as latin-1, so no request can carry `٠.٩` to `_accept_ranges` as those code points — the
guard is on the parser's own contract, not on a reachable request.
"""

from app import _accept_ranges


def test_non_ascii_decimal_digits_are_not_a_qvalue():
    """`٠.٩` and `０.９` are decimal digits Python reads and the grammar does not name."""
    for token in ("\u0660.\u0669", "\uff10.\uff19", "\u0669", "\u07c9"):
        assert _accept_ranges(f"text/plain;q={token}") == [("text/plain", 0.0)], token


def test_ascii_qvalues_still_parse():
    assert _accept_ranges("text/plain;q=0.9") == [("text/plain", 0.9)]
    assert _accept_ranges("text/plain;q=0.999") == [("text/plain", 0.999)]
    assert _accept_ranges("text/plain;q=1") == [("text/plain", 1.0)]
    assert _accept_ranges("text/plain;q=0") == [("text/plain", 0.0)]
    # Readable but out of range still clamps rather than inverting into a refusal.
    assert _accept_ranges("text/plain;q=2") == [("text/plain", 1.0)]


def test_forms_float_reads_and_the_grammar_does_not():
    for token in ("nan", "inf", "1e3", "+0.5", ".9", "0.9001"):
        assert _accept_ranges(f"text/plain;q={token}") == [("text/plain", 0.0)], token
