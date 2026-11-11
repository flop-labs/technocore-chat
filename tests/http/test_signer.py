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


def _x25519(seed: int) -> str:
    """A real X25519 public key, unpadded base64url — what `sign.py e2e` insists on."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    raw = X25519PrivateKey.from_private_bytes(bytes([seed]) * 32).public_key().public_bytes_raw()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


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
    # The script is standalone by design and cannot import didkey, so its copy of the
    # nonce rule and the server's are held together only by a gate. Derive the
    # expectation from NONCE_RE itself rather than a hand-listed pair, so the next change
    # to the pattern is caught here too (thanks @sailorpepe, #575). The probes: '١' is a
    # Unicode digit isdigit() accepts and NONCE_RE refuses, "007" and "00" are extra
    # spellings of a value the store keeps as int(nonce) and would read back differently,
    # "0" is the smallest valid counter so it must still sign, 20 digits and the empty
    # string are over- and under-length.
    for probe in ("0", "7", "007", "00", "١", "0" * 20, ""):
        out = run("say", "--seed", SEED, "lobby", probe, "hi")
        accepted = out.returncode == 0
        assert accepted == bool(didkey.NONCE_RE.fullmatch(probe)), (
            f"script and server disagree about nonce {probe!r}"
        )
        if not accepted:
            assert "nonce" in (out.stdout + out.stderr).lower()

    good = run("say", "--seed", SEED, "lobby", "7", "hi")
    assert good.returncode == 0
    did, sig = good.stdout.splitlines()
    assert did.startswith("did:key:z6Mk")
    # The server's own pattern, not a copy of it: a stale copy here would pass a
    # signature the signed lane refuses, which is the gap these tests exist to close.
    assert re.fullmatch(didkey.SIG_PATTERN, sig)


def test_the_e2e_record_the_script_prints_is_one_a_sender_accepts() -> None:
    """patterns.md pattern 4 tells a recipient to publish what `sign.py e2e` prints and a
    sender to trust only a record `e2e_key` accepts, so the two have to agree — and the
    record must verify only against the key that signed it, whatever DID a note puts first."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sign", SIGNER)
    assert spec is not None and spec.loader is not None
    sign_py = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sign_py)

    did = run("--seed", SEED, "did").stdout.strip()
    x25519 = _x25519(5)
    out = run("--seed", SEED, "e2e", x25519, "mb-p-inbox", "5")
    assert out.returncode == 0, out.stderr
    line = out.stdout.splitlines()[-1]
    assert line.startswith("e2e: ")
    assert sign_py.e2e_key(did, f"{did} {line}") == (x25519, "mb-p-inbox", 5)
    other = run("--seed", "bb" * 32, "did").stdout.strip()
    assert sign_py.e2e_key(other, f"{other} {line}") is None  # someone else's record

    for bad in (
        ["e2e", x25519, "Not A Room", "5"],
        ["e2e", x25519, "mb-p-inbox", "١"],
        ["e2e", "x", "mb-p-inbox", "5"],  # signs fine, and no sender could ever decode it
        ["e2e", "A" * 42 + "B", "mb-p-inbox", "5"],  # 32 bytes, but not the canonical spelling
        ["e2e", "A" * 43, "mb-p-inbox", "5"],  # 32 zero bytes: a low-order point, no exchange
    ):
        refused = run("--seed", SEED, *bad)
        assert refused.returncode != 0, bad

    # A record signed over a key nobody can decode is skipped, never chosen as newest.
    typo = f"e2e: x mb-p-inbox 9 {sign_py.signature(sign_py.load_key(SEED)[0], sign_py.e2e_record(did, 'x', 'mb-p-inbox', '9'))}"
    assert sign_py.e2e_key(did, f"{did} {line} {typo}") == (x25519, "mb-p-inbox", 5)


def test_e2e_key_prints_what_a_sender_seals_to_or_refuses(tmp_path) -> None:
    """The sender's half of pattern 4 as a command, since the pattern says a shell is all
    either side needs: the verified record's fields, or a nonzero exit meaning "do not seal"."""
    did = run("--seed", SEED, "did").stdout.strip()
    key = _x25519(6)
    line = run("--seed", SEED, "e2e", key, "mb-p-inbox", "5").stdout.splitlines()[-1]
    note = tmp_path / "note.txt"
    note.write_text(f"{did} x25519:{'B' * 43} mailbox:mb-p-evil {line}", encoding="utf-8")
    picked = run("e2e-key", did, str(note))
    assert picked.returncode == 0 and picked.stdout.split() == [key, "mb-p-inbox", "5"]
    note.write_text(f"{did} x25519:{'B' * 43} mailbox:mb-p-evil", encoding="utf-8")
    refused = run("e2e-key", did, str(note))
    assert refused.returncode != 0 and "do not seal" in refused.stderr


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
