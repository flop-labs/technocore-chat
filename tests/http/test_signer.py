"""scripts/sign.py's CLI is a documented contract — test it like one.

Every claim the script's own docstring makes is a promise a stranger relies on:
both --seed orders work, a keygen seed reproduces the did, every nonce the script
accepts is one the server's NONCE_RE accepts, and a signature it emits is one the
server verifies. All four PR-#54 review findings lived exactly in the gap between
those promises and anything a gate ran; these tests are the gate (issue #56).
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

import _client  # noqa: F401 (imported for the fixture alias below)

client = _client.client  # the shared TestClient fixture

ROOT = Path(__file__).resolve().parents[2]
SIGNER = ROOT / "scripts" / "sign.py"
# The project venv carries cryptography (the signed lane's own dep), so the script
# runs under the same interpreter as the suite — no uv provisioning needed here.
SEED = "aa" * 32


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SIGNER), *args], capture_output=True, text=True, cwd=ROOT
    )


def test_both_documented_seed_orders_agree() -> None:
    before = run("--seed", SEED, "did")
    after = run("did", "--seed", SEED)
    assert before.returncode == 0 and after.returncode == 0
    assert before.stdout == after.stdout
    assert before.stdout.startswith("did:key:z6Mk")


def test_a_keygen_seed_reproduces_the_did() -> None:
    out = run("keygen")
    assert out.returncode == 0
    seed = next(
        line.removeprefix("seed: ") for line in out.stdout.splitlines() if line.startswith("seed: ")
    )
    did = next(
        line.removeprefix("did:  ") for line in out.stdout.splitlines() if line.startswith("did:  ")
    )
    again = run("did", "--seed", seed)
    assert again.returncode == 0 and again.stdout.strip() == did


def test_nonces_are_rejected_exactly_where_the_server_would_reject() -> None:
    # '١' is a Unicode digit isdigit() accepts and NONCE_RE ([0-9]{1,19}) refuses;
    # 20 digits and the empty string are over- and under-length. The script must
    # refuse to sign all three — a signature we emit must be submittable.
    for bad_nonce in ("١", "0" * 20, ""):
        out = run("say", "--seed", SEED, "lobby", bad_nonce, "hi")
        assert out.returncode != 0, f"nonce {bad_nonce!r} was accepted"
        assert "nonce" in (out.stdout + out.stderr).lower()

    good = run("say", "--seed", SEED, "lobby", "7", "hi")
    assert good.returncode == 0
    did, sig = good.stdout.splitlines()
    assert did.startswith("did:key:z6Mk")
    assert re.fullmatch(r"[A-Za-z0-9_-]{86}", sig)


def test_a_script_signature_is_accepted_by_the_real_server(client) -> None:
    text = "hello from the signer"
    out = run("say", "--seed", SEED, "signerroom", "3", text)
    assert out.returncode == 0
    did, sig = out.stdout.splitlines()
    r = client.get(f"/r/signerroom/say-signed/{did}/{sig}/3/hello%20from%20the%20signer")
    assert r.status_code == 200, r.text
    assert text in r.text
    assert "<z6Mk" in r.text  # a verified writer renders as the key, not a nickname


def test_the_scripts_sweep_still_matches_the_servers(client) -> None:
    """The script's copy of the sweep is load-bearing, so gate it like the rest.

    `scripts/sign.py` re-declares `INVISIBLE_CATEGORIES` instead of importing
    `store`'s — deliberately, because it must run standalone with only
    `cryptography` beside it. Nothing asserted the two agree, and every other
    signer test signs plain ASCII, so a category added on one side and not the
    other passes the whole suite while every swept character a real caller sends
    starts returning 403.

    One character from each swept category, signed by the script and submitted
    raw: the server verifies against what it stores, so the copies agreeing is
    the only reason this is a 200. `Cs` is absent because a lone surrogate has no
    UTF-8 encoding and cannot survive an argv round-trip — the other five carry
    the guard.
    """
    import store

    raw = "a\x01b​cd e f"
    covered = {unicodedata.category(c) for c in raw} & set(store.INVISIBLE_CATEGORIES)
    assert covered == set(store.INVISIBLE_CATEGORIES) - {"Cs"}, (
        "a category joined the server's sweep with no character here to exercise it"
    )

    out = run("say", "--seed", SEED, "sweeproom", "5", raw)
    assert out.returncode == 0, out.stderr
    did, sig = out.stdout.splitlines()

    # percent-encoded, not raw: a C0 control is a legal path segment only escaped,
    # and the HTTP client refuses to send one literally.
    r = client.get(f"/r/sweeproom/say-signed/{did}/{sig}/5/{quote(raw, safe='')}")
    assert r.status_code == 200, (
        f"the script signed bytes the server did not store ({r.status_code}) — "
        f"scripts/sign.py's INVISIBLE_CATEGORIES has drifted from store's"
    )
    assert client.get("/r/sweeproom?format=json").json()["messages"][0]["text"] == store.clean_text(
        raw
    )
