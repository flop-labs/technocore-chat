import base64
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"

BASE_URL = "http://127.0.0.1:8000"


def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = ""

    while n:
        n, remainder = divmod(n, 58)
        result = B58[remainder] + result

    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + result


def make_did(private_key: Ed25519PrivateKey) -> str:
    public_key = private_key.public_key().public_bytes_raw()
    encoded = base58_encode(MULTICODEC_ED25519 + public_key)
    return "did:key:z" + encoded


private_key = Ed25519PrivateKey.generate()
did = make_did(private_key)

room = "lobby"
nonce = 1
original_text = "hello from the signed client"
modified_text = "hello from the hacked client"

canonical = f"{room}|{nonce}|{original_text}"
signature = base64.urlsafe_b64encode(
    private_key.sign(canonical.encode("utf-8"))
).decode().rstrip("=")

url = (
    f"{BASE_URL}/r/{urllib.parse.quote(room, safe='')}"
    f"/say-signed/{urllib.parse.quote(did, safe='')}"
    f"/{urllib.parse.quote(signature, safe='')}"
    f"/{nonce}"
    f"/{urllib.parse.quote(modified_text, safe='')}"
)

try:
    urllib.request.urlopen(url)
except urllib.error.HTTPError as exc:
    assert exc.code == 403, f"expected 403, got {exc.code}"
    print("PASS: modified signed content was rejected with HTTP 403")
else:
    raise AssertionError("server accepted content that was not covered by the signature")