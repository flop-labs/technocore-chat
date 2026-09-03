"""Conservative, dependency-free screening for content read from public rooms.

This is deliberately a triage helper, not a moderation or truth oracle.  It only reports
high-signal patterns that should make an agent stop and inspect a message as data.  The
room remains world-writable and the caller remains responsible for the decision to act.
"""

from __future__ import annotations

import re
import unicodedata

_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "α": "a",
        "β": "b",
        "ε": "e",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "ν": "v",
        "κ": "k",
    }
)
_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|former|above)\s+"
    r"(?:instructions|rules|directives|prompts)\b|"
    r"\b(?:system\s+override|developer\s+mode|new\s+system\s+directive)\b|"
    r"(?:print|reveal|show|send|leak)\s+(?:your\s+|the\s+)?(?:system\s+prompt|private\s+key|seed\s+phrase|api\s+key)\b|"
    r"<\s*\|\s*im_(?:start|end)\s*\|>|\[\s*/?\s*INST\s*\]",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"\b0x[a-f0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b", re.IGNORECASE)
_PHISHING = re.compile(
    r"https?://[^\s/]*(?:claim|airdrop|reward|presale|wallet)[^\s]*", re.IGNORECASE
)
_DID = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")
_RESERVED = {"admin", "administrator", "root", "server", "system", "technocore_admin", "flop_team"}


def _normalise(text: str) -> str:
    # Match the service's clean_text boundary: swept characters become spaces, rather
    # than disappearing.  Removing them can join two otherwise separate words and make
    # this local triage disagree with the text readers will actually see.  Casefold before
    # the small confusable map so uppercase Cyrillic/Greek lookalikes are covered too.
    swept = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"} else char
        for char in text
    )
    return " ".join(unicodedata.normalize("NFKC", swept).casefold().translate(_CONFUSABLES).split())


def _provenance(sender: str | None) -> str:
    if sender and _DID.fullmatch(sender):
        return "verified_did"
    if sender and sender.lstrip("~").lower() in _RESERVED:
        return "impersonator_warning"
    return "unverified_nick"


def scan(text: str, sender: str | None = None) -> dict[str, str]:
    """Return a small machine-readable triage result for one untrusted message."""
    normalised = _normalise(text)
    if _INJECTION.search(normalised):
        return {
            "verdict": "threat",
            "reason": "prompt_injection",
            "provenance": _provenance(sender),
        }
    if _TOKEN.search(normalised):
        return {
            "verdict": "suspicious",
            "reason": "unverified_token_contract",
            "provenance": _provenance(sender),
        }
    if _PHISHING.search(normalised):
        return {
            "verdict": "suspicious",
            "reason": "phishing_link",
            "provenance": _provenance(sender),
        }
    if sender and sender.lstrip("~").lower() in _RESERVED:
        return {
            "verdict": "threat",
            "reason": "impersonation_nick",
            "provenance": "impersonator_warning",
        }
    return {
        "verdict": "clean",
        "reason": "no_high_signal_match",
        "provenance": _provenance(sender),
    }
