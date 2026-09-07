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
from pathlib import Path

import _client  # noqa: F401 (imported for the fixture alias below)

import didkey

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
    # The server's own pattern, not a copy of it: a stale copy here would pass a
    # signature the signed lane refuses, which is the gap these tests exist to close.
    assert re.fullmatch(didkey.SIG_PATTERN, sig)


def test_a_script_signature_is_accepted_by_the_real_server(client) -> None:
    text = "hello from the signer"
    out = run("say", "--seed", SEED, "signerroom", "3", text)
    assert out.returncode == 0
    did, sig = out.stdout.splitlines()
    r = client.get(f"/r/signerroom/say-signed/{did}/{sig}/3/hello%20from%20the%20signer")
    assert r.status_code == 200, r.text
    assert text in r.text
    assert "<z6Mk" in r.text  # a verified writer renders as the key, not a nickname


def test_a_stored_signed_record_keeps_its_signature(client) -> None:
    """Issue #66: verifying a write and then dropping the signature leaves a record
    nobody can check. The canonical string is rebuildable from the record, so the
    signature is the only missing piece — and without it `from` is a claim about what
    the server did, not something a reader can confirm."""
    import didkey

    did, sign = _client._keypair()
    text = "41 rooms at 20:31Z"
    assert _client._say_signed(client, "sigroom", did, sign, text).status_code == 200

    rec = client.get("/r/sigroom?format=json").json()["messages"][-1]
    assert rec["sig"], "a verified record must carry the signature it was accepted on"
    didkey.verify(did, rec["sig"], f"sigroom|{rec['nonce']}|{rec['text']}")


def test_both_signed_lanes_store_the_signature(client) -> None:
    """Lane parity: the POST body lane and the GET path lane must record the same thing."""
    import didkey

    did, sign = _client._keypair()
    posted = _client._post_signed(client, "sigroom2", did, sign, "through the body lane")
    assert posted.status_code == 200

    rec = client.get("/r/sigroom2?format=json").json()["messages"][-1]
    didkey.verify(did, rec["sig"], f"sigroom2|{rec['nonce']}|{rec['text']}")
