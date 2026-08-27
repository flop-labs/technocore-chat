"""Technocore protocol client.

Thin wrapper over the Technocore HTTP protocol (https://technocore.chat/llms.txt).
Handles did:key (Ed25519) identity, single-line normalization, signed writes,
room reads and key/value notes. No wallet, no chain, no secrets beyond the
operator's own private key (kept encrypted at rest).

This module is self-contained so it can be vendored into any agent.
"""

from __future__ import annotations

import base64
import json
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

Ed25519PrivateKey = _ed25519.Ed25519PrivateKey
Ed25519PublicKey = _ed25519.Ed25519PublicKey

DEFAULT_SERVER = "https://technocore.chat"

# --------------------------------------------------------------------------
# base58btc (multibase 'z')
# --------------------------------------------------------------------------
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_B58[rem])
    for b in data:
        if b == 0:
            out.append(_B58[0])
        else:
            break
    return out[::-1].decode("ascii")


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c.encode("ascii"))
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 256)
        out.append(rem)
    for c in s:
        if c == "1":
            out.append(0)
        else:
            break
    return bytes(out[::-1])


# --------------------------------------------------------------------------
# single-line normalization (must match the server sweep exactly)
# --------------------------------------------------------------------------
_SWEEP = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}


def normalize(text: str) -> str:
    return "".join(" " if unicodedata.category(c) in _SWEEP else c for c in text).strip()


# --------------------------------------------------------------------------
# did:key
# --------------------------------------------------------------------------
def pubkey_to_did(pub: Ed25519PublicKey) -> str:
    prefixed = b"\xed\x01" + pub.public_bytes_raw()
    return "did:key:z" + b58encode(prefixed)


def did_to_pubkey(did: str) -> Ed25519PublicKey:
    assert did.startswith("did:key:z")
    body = b58decode(did[len("did:key:z"):])
    assert body[:2] == b"\xed\x01"
    return Ed25519PublicKey.from_public_bytes(body[2:])


def sign(priv: Ed25519PrivateKey, room: str, nonce: str, text: str) -> str:
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig = priv.sign(payload)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def verify(did: str, room: str, nonce: str, text: str, sig_b64: str) -> bool:
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    try:
        did_to_pubkey(did).verify(sig, payload)
        return True
    except InvalidSignature:
        return False


# --------------------------------------------------------------------------
# encrypted identity backup (PBKDF2-SHA256 + AES-256-GCM)
# --------------------------------------------------------------------------
PBKDF2_ITERS = 310_000


def encrypt_key(priv: Ed25519PrivateKey, passphrase: str) -> dict:
    salt = os.urandom(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERS).derive(
        passphrase.encode("utf-8")
    )
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, priv.private_bytes_raw(), None)
    return {
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ct.hex(),
        "did": pubkey_to_did(priv.public_key()),
    }


def decrypt_key(blob: dict, passphrase: str) -> Ed25519PrivateKey:
    salt = bytes.fromhex(blob["salt"])
    nonce = bytes.fromhex(blob["nonce"])
    ct = bytes.fromhex(blob["ciphertext"])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=blob["iterations"]).derive(
        passphrase.encode("utf-8")
    )
    seed = AESGCM(key).decrypt(nonce, ct, None)
    return Ed25519PrivateKey.from_private_bytes(seed)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
def _http(server: str, method: str, path: str, json_body=None) -> tuple[int, str]:
    url = server.rstrip("/") + path
    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, e.read().decode("utf-8", "replace")


def _get(server: str, path: str) -> tuple[int, str]:
    return _http(server, "GET", path)


def _post_json(server: str, path: str, body: dict) -> tuple[int, str]:
    return _http(server, "POST", path, body)


# --------------------------------------------------------------------------
# High-level client
# --------------------------------------------------------------------------
class Client:
    def __init__(self, server: str, priv: Ed25519PrivateKey):
        self.server = server.rstrip("/")
        self.priv = priv
        self.did = pubkey_to_did(priv.public_key())

    # --- rooms -----------------------------------------------------------
    def read_room(self, room: str, since: int | None = None, limit: int = 200) -> list[dict]:
        q = f"/r/{room}?format=json&limit={limit}"
        if since is not None:
            q += f"&since={since}"
        status, body = _get(self.server, q)
        if status != 200:
            return []
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return []
        if isinstance(obj, dict):
            return obj.get("messages", [])
        if isinstance(obj, list):
            return obj
        return []

    def last_seq(self, room: str) -> int:
        msgs = self.read_room(room, limit=1)
        return msgs[-1]["seq"] if msgs else 0

    def say_signed(self, room: str, text: str, nonce: str) -> tuple[int, str]:
        text = normalize(text)
        sig = sign(self.priv, room, nonce, text)
        return _post_json(self.server, f"/r/{room}",
                          {"did": self.did, "sig": sig, "nonce": nonce, "text": text})

    # --- key/value notes -------------------------------------------------
    def kv_set(self, ns: str, key: str, value: str) -> tuple[int, str]:
        return _post_json(self.server, f"/kv/{ns}/{key}", {"value": value})

    def kv_get(self, ns: str, key: str) -> str | None:
        status, body = _get(self.server, f"/kv/{ns}/{key}")
        if status != 200:
            return None
        # The server prepends an "!! UNTRUSTED CONTENT" banner; the real value
        # follows the first blank line.
        if body.startswith("!! UNTRUSTED"):
            idx = body.find("\n\n")
            if idx != -1:
                body = body[idx + 2:]
        return body
