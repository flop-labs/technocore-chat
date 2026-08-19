import base64
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# Base58btc alphabet used by did:key.
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# multicodec prefix for Ed25519 public keys.
MULTICODEC_ED25519 = b"\xed\x01"


def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = ""

    while n:
        n, remainder = divmod(n, 58)
        result = B58[remainder] + result

    # Preserve leading zero bytes.
    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + result


def make_did(private_key: Ed25519PrivateKey) -> str:
    public_key = private_key.public_key().public_bytes_raw()
    encoded = base58_encode(MULTICODEC_ED25519 + public_key)
    return "did:key:z" + encoded


def sign(private_key: Ed25519PrivateKey, message: str) -> str:
    signature = private_key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

BASE_URL = "http://127.0.0.1:8000"
ROOM = "lobby"
NONCE = 1
TEXT = "hello from my tiny signed client"


# ------------------------------------------------------------
# Generate key + DID
# ------------------------------------------------------------

private_key = Ed25519PrivateKey.generate()
did = make_did(private_key)

print("DID:")
print(did)
print()


# ------------------------------------------------------------
# Build the exact message that Technocore signs
# ------------------------------------------------------------

canonical = f"{ROOM}|{NONCE}|{TEXT}"

print("Canonical message:")
print(canonical)
print()


# ------------------------------------------------------------
# Sign it
# ------------------------------------------------------------

signature = sign(private_key, canonical)

print("Signature:")
print(signature)
print()


# ------------------------------------------------------------
# Call say-signed
# ------------------------------------------------------------

url = (
    f"{BASE_URL}/r/{urllib.parse.quote(ROOM, safe='')}"
    f"/say-signed/{urllib.parse.quote(did, safe='')}"
    f"/{urllib.parse.quote(signature, safe='')}"
    f"/{NONCE}"
    f"/{urllib.parse.quote(TEXT, safe='')}"
)

print("Request URL:")
print(url)
print()

with urllib.request.urlopen(url) as response:
    print("HTTP status:", response.status)
    print("Server response:")
    print(response.read().decode())