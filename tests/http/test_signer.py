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


def test_a_script_signature_is_accepted_by_the_real_server(client) -> None:
    text = "hello from the signer"
    out = run("say", "--seed", SEED, "signerroom", "3", text)
    assert out.returncode == 0
    did, sig = out.stdout.splitlines()
    r = client.get(f"/r/signerroom/say-signed/{did}/{sig}/3/hello%20from%20the%20signer")
    assert r.status_code == 200, r.text
    assert text in r.text
    assert "<z6Mk" in r.text  # a verified writer renders as the key, not a nickname


def test_init_creates_a_reusable_protected_identity_without_printing_seed(tmp_path: Path) -> None:
    identity = tmp_path / "nested" / "identity.json"
    created = run("init", "--identity", str(identity))
    assert created.returncode == 0, created.stderr
    assert identity.exists()
    assert "seed" not in created.stdout.lower()
    did = next(
        line.removeprefix("did:      ")
        for line in created.stdout.splitlines()
        if line.startswith("did:      ")
    )
    reused = run("did", "--identity", str(identity))
    assert reused.returncode == 0 and reused.stdout.strip() == did
    if sys.platform != "win32":
        assert identity.stat().st_mode & 0o777 == 0o600


def test_init_never_overwrites_an_existing_identity(tmp_path: Path) -> None:
    identity = tmp_path / "identity.json"
    first = run("init", "--identity", str(identity))
    before = identity.read_bytes()
    second = run("init", "--identity", str(identity))
    assert first.returncode == 0 and second.returncode != 0
    assert identity.read_bytes() == before
    assert "already exists" in second.stderr


def test_identity_signatures_match_the_seed_and_detect_tampering(tmp_path: Path) -> None:
    import json

    identity = tmp_path / "identity.json"
    assert run("init", "--identity", str(identity)).returncode == 0
    data = json.loads(identity.read_text())
    from_identity = run("say", "--identity", str(identity), "lobby", "7", "hi")
    from_seed = run("say", "--seed", data["seed"], "lobby", "7", "hi")
    assert from_identity.returncode == 0 and from_identity.stdout == from_seed.stdout

    data["did"] = "did:key:z6Mk" + "1" * 44
    identity.write_text(json.dumps(data))
    tampered = run("did", "--identity", str(identity))
    assert tampered.returncode != 0
    assert "does not match seed" in tampered.stderr


def test_seed_and_identity_are_mutually_exclusive(tmp_path: Path) -> None:
    identity = tmp_path / "identity.json"
    assert run("init", "--identity", str(identity)).returncode == 0
    out = run("did", "--seed", SEED, "--identity", str(identity))
    assert out.returncode != 0
    assert "choose one key source" in out.stderr
