"""The signing constants /humans restates, pinned to the ones the server verifies against.

The page signs in the browser: it builds `room|nonce|swept-text`, signs it with an Ed25519
key held in `localStorage`, and posts `did`/`sig`/`nonce` to the lane `app.room_post`
already had. Nothing on the server changed for that, which is the point — but it means the
page now carries a second spelling of five things `didkey.py`, `store.py` and
`scripts/sign.py` also spell, and a second spelling is a thing that can drift.

Drift here is quiet and it fails at exactly the wrong moment. Every constant below is used
only on the *send* path, so a wrong byte produces a page that loads, renders, polls and
lists rooms perfectly, and then answers 403 the first time somebody signed in tries to say
something. No Python test of the server catches that: the server is right. The browser
probe (tests/humans_ui_probe.mjs) does catch it, and it is not in CI because it needs
Chromium.

So: read the constants back out of the served page and check them here, where they cost
nothing. Regex extraction rather than a JavaScript engine, for the same reason
test_humans_name_input_limit.py reads `maxlength` that way — the assertion is about a value,
and standing up a JS runtime to read one array is a dependency this suite does not have.

Run: uv run --group dev python -m pytest tests/unit/test_humans_identity.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_der_private_key
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _signer():
    """scripts/sign.py, loaded by path the way test_store_doc.py loads its generator.

    It is a PEP 723 standalone with its own dependency header and no package around it, so
    neither type checking nor a bare pytest run has scripts/ on the module search path —
    and it must stay that way, since the whole point of that file is that it runs with no
    checkout.
    """
    spec = importlib.util.spec_from_file_location("sign", ROOT / "scripts" / "sign.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def page() -> str:
    """The page as served, not the file on disk — the same thing a browser would parse."""
    import app as app_module

    return TestClient(app_module.app).get("/humans").text


def _byte_array(html: str, name: str) -> bytes:
    """The `var NAME = [0x.., ...]` literal the page declares, as bytes."""
    match = re.search(rf"var {name} = \[([^\]]+)\]", html)
    assert match is not None, f"missing {name} in the served page"
    return bytes(int(b, 16) for b in re.findall(r"0x([0-9a-f]{2})", match.group(1)))


def test_the_pkcs8_prefix_the_page_wraps_a_seed_in_is_a_real_ed25519_key(page):
    """The one constant with no counterpart in this repo to compare against.

    WebCrypto will not import a raw Ed25519 seed, so the page prepends a fixed RFC 8410 §7
    header and imports the result as PKCS#8. That header is 16 hand-written bytes of DER; if
    any of them is wrong the browser throws on import, the identity row silently never
    appears, and the page looks exactly like a browser without Ed25519. Rebuilding a key
    from it here with a different library is what makes that a build failure instead.
    """
    prefix = _byte_array(page, "PKCS8_ED25519")
    seed = bytes(range(32))

    loaded = load_der_private_key(prefix + seed, password=None)
    assert isinstance(loaded, Ed25519PrivateKey)
    # Round-trip the whole way: the key the header produces must be the key the seed means,
    # not merely *a* valid key that parsed.
    assert loaded.private_bytes_raw() == seed


def test_the_did_the_page_would_render_matches_the_signer_that_ships_beside_it(page):
    """did:key is derived in two places now — scripts/sign.py for the command line and the
    page for the browser — and a reader who moves one seed between them must land on one
    identity. That is the whole promise of pasting a seed into the composer, so it is
    asserted rather than assumed."""
    import didkey

    sign = _signer()
    assert _byte_array(page, "MULTICODEC_ED25519") == didkey.MULTICODEC_ED25519

    # The page's base58 alphabet, read back out of the served bytes.
    alphabet = re.search(r"var B58 = '([^']+)'", page)
    assert alphabet is not None and alphabet.group(1) == sign.B58

    # And the composition those two feed: prefix, base58, `did:key:z`. Computed here the way
    # the page computes it, and compared against the signer's own answer for the same seed.
    seed = bytes(range(32))
    key = Ed25519PrivateKey.from_private_bytes(seed)
    expected = sign.did_of(key)

    raw = didkey.MULTICODEC_ED25519 + key.public_key().public_bytes_raw()
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = sign.B58[rem] + out
    assert "did:key:z" + out == expected
    # ...and the server agrees it is a DID it would verify against.
    assert didkey.is_did(expected)


def test_the_pages_sweep_covers_exactly_the_categories_the_server_replaces(page):
    """The signature covers the text the server *stores*, so the page sweeps before signing.

    A category the page misses is a 403 the reader cannot explain; a category it adds that
    the server does not have is a message whose stored text differs from what was typed for
    no reason. Both are silent, and both are one missing pair of characters in a regex.
    """
    import store

    match = re.search(r"var INVISIBLE = /\[([^\]]+)\]/gu", page)
    assert match is not None, "missing the INVISIBLE sweep in the served page"
    categories = set(re.findall(r"\\p\{(\w+)\}", match.group(1)))
    assert categories == set(store.INVISIBLE_CATEGORIES)


def test_the_page_only_offers_seeds_the_command_line_signer_would_also_accept(page):
    """scripts/sign.py takes 64 hex characters *or* hashes anything else into a seed. The
    page deliberately takes only the first: hashing whatever was pasted turns a mistyped
    seed into a different, perfectly working identity, and the reader has no way to see that
    they are now somebody else. Asserted because the looser rule is the tempting one.
    """
    match = re.search(r"var SEED_RE = /([^/]+)/i", page)
    assert match is not None and match.group(1) == r"^[0-9a-f]{64}$"
