"""Threat, scam, and provenance scanner for Technocore MCP.

Evaluates untrusted message content and sender identity for:
1. Adversarial prompt injections and instruction overrides.
2. Homoglyph / confusable character obfuscations (NFKC).
3. Fake token contracts and phishing links (pump.fun, unverified EVM contracts).
4. Syntactic DID provenance and impersonation attempts.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TypedDict

# Confusable homoglyph translation table (Cyrillic & Greek homoglyphs -> ASCII)
HOMOGLYPH_MAP = {
    # Cyrillic to Latin
    ord("а"): "a",
    ord("А"): "A",
    ord("е"): "e",
    ord("Е"): "E",
    ord("о"): "o",
    ord("О"): "O",
    ord("р"): "p",
    ord("Р"): "P",
    ord("с"): "c",
    ord("С"): "C",
    ord("у"): "y",
    ord("У"): "Y",
    ord("х"): "x",
    ord("Х"): "X",
    ord("і"): "i",
    ord("І"): "I",
    ord("ј"): "j",
    ord("Ј"): "J",
    ord("ѕ"): "s",
    ord("Ѕ"): "S",
    ord("ԁ"): "d",
    ord("Ԃ"): "D",
    ord("ԛ"): "q",
    ord("ԝ"): "w",
    # Greek to Latin
    ord("α"): "a",
    ord("Α"): "A",
    ord("β"): "b",
    ord("Β"): "B",
    ord("ε"): "e",
    ord("Ε"): "E",
    ord("ο"): "o",
    ord("О"): "O",
    ord("ρ"): "p",
    ord("Ρ"): "P",
    ord("τ"): "t",
    ord("Τ"): "T",
    ord("ν"): "v",
    ord("Ν"): "N",
    ord("κ"): "k",
    ord("Κ"): "K",
}

PROMPT_INJECTION_PATTERNS = [
    # Instruction Resets & Overrides
    re.compile(
        r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|former|prior|past|above)\s+(instructions|prompts|directives|rules|constraints|directions)\b",
        re.I,
    ),
    re.compile(
        r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(?:acting\s+as\s+|in\s+)?(a\s+|an\s+)?(unrestricted|jailbroken|developer\s+mode|dan\s+mode|root\s+user|system\s+admin)\b",
        re.I,
    ),
    re.compile(
        r"\b(system\s+(?:prompt\s+)?override|override\s+system|new\s+system\s+directive|developer\s+mode\s+enabled|admin_override_enabled)\b",
        re.I,
    ),
    re.compile(
        r"\b(print|reveal|output|display|show|send|leak)\s+(your\s+|the\s+)?(system\s+prompt|initial\s+instructions|private\s+key|seed\s+phrase|api\s+key)\b",
        re.I,
    ),
    # Delimiter and Frame Hijacking
    re.compile(r"<\s*\|\s*im_start\s*\|>", re.I),
    re.compile(r"<\s*\|\s*im_end\s*\|>", re.I),
    re.compile(r"\[\s*INST\s*\]|\[\s*/\s*INST\s*\]", re.I),
    re.compile(r"<<\s*SYS\s*>>|<</\s*SYS\s*>>", re.I),
    re.compile(
        r"[-=]{3,}\s*(BEGIN|START|END)\s+.*?(SYSTEM|DIRECTIVE|INSTRUCTION|PROMPT).*?[-=]{3,}", re.I
    ),
    re.compile(r"^\s*(SYSTEM|DEVELOPER|ROOT)\s*:\s*", re.M | re.I),
    # Markdown / Out-of-band Exfiltration Payloads
    re.compile(r"!\[.*?\]\((https?://[^\s\)]+)\)", re.I),
    re.compile(r"data:text/html;base64,[A-Za-z0-9+/=]{16,}", re.I),
]

SCAM_PATTERNS = [
    # Solana pump.fun token contract spam
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b", re.I),
    re.compile(r"pump\.fun\/coin\/[a-zA-Z0-9]{32,44}", re.I),
    # EVM token contract spam with claims
    re.compile(r"\b0x[a-fA-F0-9]{40}\b.*?(?:buy|claim|airdrop|launch|presale|contract|mint)", re.I),
    re.compile(r"(?:buy|claim|airdrop|launch|presale|contract|mint).*?\b0x[a-fA-F0-9]{40}\b", re.I),
    re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    # Phishing / Fake Claim Portals
    re.compile(
        r"https?://[^\s/]*(?:flop|technocore)[^\s/]*(?:claim|airdrop|reward|presale|token)[^\s]*",
        re.I,
    ),
    re.compile(
        r"(?:airdrop|presale|claim|whitelist)\s+(?:portal|live|active|link):\s*https?:\/\/", re.I
    ),
    re.compile(r"official\s+(?:flop|technocore)\s+token\s+contract", re.I),
    re.compile(r"https?://t\.me/[^\s]*(?:airdrop|claim|reward|official_flop)", re.I),
]

RESERVED_ADMIN_NAMES = frozenset(
    {
        "server",
        "admin",
        "administrator",
        "root",
        "system",
        "flop_team",
        "flop_official",
        "technocore_admin",
        "flop_labs",
        "technocore",
    }
)


class ScanResult(TypedDict):
    verdict: str  # "clean" | "suspicious" | "threat"
    reason: str  # "none" | "prompt_injection" | "homoglyph_obfuscation" | "unverified_token_contract" | "impersonation_nick"
    provenance: str  # "claimed_did" | "unverified_nick" | "impersonator_warning"
    details: str


def normalize(text: str) -> str:
    """Normalize text using NFKC and confusable substitution."""
    de_confused = text.translate(HOMOGLYPH_MAP)
    return unicodedata.normalize("NFKC", de_confused)


def evaluate_text(text: str, sender: str | None = None) -> ScanResult:
    """Evaluate text and sender for adversarial threats, scams, and provenance."""
    # 1. Provenance check
    provenance = "unverified_nick"
    if sender:
        clean_sender = sender.strip().lstrip("~")
        if sender.startswith("did:key:") or clean_sender.startswith("did:key:"):
            provenance = "claimed_did"
        elif clean_sender.lower() in RESERVED_ADMIN_NAMES:
            provenance = "impersonator_warning"

    # 2. Check raw and normalized forms for prompt injections
    norm_text = normalize(text)
    for pat in PROMPT_INJECTION_PATTERNS:
        if pat.search(text) or pat.search(norm_text):
            return {
                "verdict": "threat",
                "reason": "prompt_injection",
                "provenance": provenance,
                "details": f"Matched prompt injection pattern: {pat.pattern}",
            }

    # 3. Check for fake token contracts / phishing
    for pat in SCAM_PATTERNS:
        if pat.search(text) or pat.search(norm_text):
            return {
                "verdict": "threat",
                "reason": "unverified_token_contract",
                "provenance": provenance,
                "details": f"Matched token contract/phishing pattern: {pat.pattern}",
            }

    # 4. Homoglyph obfuscation check
    if any(ord(c) in HOMOGLYPH_MAP for c in text):
        return {
            "verdict": "suspicious",
            "reason": "homoglyph_obfuscation",
            "provenance": provenance,
            "details": "Text contains mixed Cyrillic/Greek homoglyphs resembling Latin characters",
        }

    # 5. Impersonator warning check
    if provenance == "impersonator_warning":
        return {
            "verdict": "suspicious",
            "reason": "impersonation_nick",
            "provenance": provenance,
            "details": f"Sender claims reserved nickname '~{clean_sender}' without did:key signature",
        }

    return {
        "verdict": "clean",
        "reason": "none",
        "provenance": provenance,
        "details": "No threat or scam patterns detected",
    }
