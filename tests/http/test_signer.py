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


def test_names_are_rejected_exactly_where_the_server_would_reject() -> None:
    cases = [
        ("say", ("UPPER", "7", "hi"), "room"),
        ("say", ("mb-FOO", "7", "hi"), "room"),
        ("set", ("Room", "d-owned", "7", "value"), "ns"),
        ("set", ("room-owners", "D-owned", "7", "value"), "key"),
        ("set", ("room-owners", "x" * 49, "7", "value"), "key"),
    ]
    for cmd, args, label in cases:
        out = run(cmd, "--seed", SEED, *args)
        combined = out.stdout + out.stderr
        assert out.returncode != 0, f"{cmd} accepted {args!r}"
        assert label in combined and "must match" in combined
        assert "did:key:" not in out.stdout
        assert not any(re.fullmatch(r"[A-Za-z0-9_-]{86}", line) for line in out.stdout.splitlines())


def test_a_script_signature_is_accepted_by_the_real_server(client) -> None:
    text = "hello from the signer"
    out = run("say", "--seed", SEED, "signerroom", "3", text)
    assert out.returncode == 0
    did, sig = out.stdout.splitlines()
    r = client.get(f"/r/signerroom/say-signed/{did}/{sig}/3/hello%20from%20the%20signer")
    assert r.status_code == 200, r.text
    assert text in r.text
    assert "<z6Mk" in r.text  # a verified writer renders as the key, not a nickname


def test_a_script_note_signature_is_accepted_by_the_real_server(client) -> None:
    did = run("did", "--seed", SEED).stdout.strip()
    out = run("set", "--seed", SEED, "room-owners", "d-signer", "1", did)
    assert out.returncode == 0
    signed_did, sig = out.stdout.splitlines()
    assert signed_did == did
    r = client.get(f"/kv/room-owners/d-signer/set-signed/{did}/{sig}/1/{did}?if_absent=1")
    assert r.status_code == 200, r.text
    assert client.get("/kv/room-owners/d-signer").text.strip().endswith(did)
