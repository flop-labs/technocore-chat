"""scripts/sign.py's CLI is a documented contract — test it like one.

Every claim the script's own docstring makes is a promise a stranger relies on:
both --seed orders work, a keygen seed reproduces the did, every nonce the script
accepts is one the server's NONCE_RE accepts, and a signature it emits is one the
server verifies. All four PR-#54 review findings lived exactly in the gap between
those promises and anything a gate ran; these tests are the gate (issue #56).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import _client  # noqa: F401 (imported for the fixture alias below)
import pytest

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


def test_a_keygen_seed_file_hides_the_seed_and_reproduces_the_did(tmp_path) -> None:
    seed_file = tmp_path / "identity.seed"
    out = run("keygen", "--seed-file", str(seed_file))
    assert out.returncode == 0
    assert not any(line.startswith("seed: ") for line in out.stdout.splitlines())
    did = next(
        line.removeprefix("did:  ") for line in out.stdout.splitlines() if line.startswith("did:  ")
    )

    before = run("--seed-file", str(seed_file), "did")
    after = run("did", "--seed-file", str(seed_file))
    assert before.returncode == 0 and after.returncode == 0
    assert before.stdout.strip() == after.stdout.strip() == did
    if os.name != "nt":
        assert seed_file.stat().st_mode & 0o777 == 0o600


def test_keygen_refuses_to_replace_a_seed_file(tmp_path) -> None:
    seed_file = tmp_path / "identity.seed"
    first = run("keygen", "--seed-file", str(seed_file))
    original = seed_file.read_bytes()
    second = run("keygen", "--seed-file", str(seed_file))
    assert first.returncode == 0 and second.returncode != 0
    assert seed_file.read_bytes() == original
    assert "cannot create seed file" in (second.stdout + second.stderr)


def test_a_posix_seed_file_must_be_private(tmp_path) -> None:
    if os.name == "nt":
        return
    seed_file = tmp_path / "identity.seed"
    seed_file.write_text(SEED + "\n", encoding="utf-8")
    seed_file.chmod(0o644)
    out = run("did", "--seed-file", str(seed_file))
    assert out.returncode != 0
    assert "chmod 600" in (out.stdout + out.stderr)


def test_seed_and_seed_file_are_mutually_exclusive_across_option_orders(tmp_path) -> None:
    seed_file = tmp_path / "identity.seed"
    seed_file.write_text(SEED + "\n", encoding="utf-8")
    out = run("--seed-file", str(seed_file), "did", "--seed", SEED)
    assert out.returncode != 0
    assert "mutually exclusive" in (out.stdout + out.stderr)


def test_seed_file_signatures_match_the_existing_seed_input(tmp_path) -> None:
    seed_file = tmp_path / "identity.seed"
    seed_file.write_text(SEED + "\n", encoding="utf-8")
    if os.name != "nt":
        seed_file.chmod(0o600)
    from_file = run("say", "--seed-file", str(seed_file), "lobby", "7", "hello")
    from_argument = run("say", "--seed", SEED, "lobby", "7", "hello")
    assert from_file.returncode == 0 and from_argument.returncode == 0
    assert from_file.stdout == from_argument.stdout


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_seed_file_overrides_environment_and_accepts_line_endings(tmp_path, monkeypatch, ending):
    seed_file = tmp_path / "identity.seed"
    seed_file.write_bytes(SEED.encode() + ending)
    seed_file.chmod(0o600)
    monkeypatch.setenv("SIGN_SEED", "bb" * 32)
    out = run("did", "--seed-file", str(seed_file))
    expected = run("did", "--seed", SEED)
    assert out.returncode == expected.returncode == 0
    assert out.stdout == expected.stdout
    assert SEED not in out.stdout + out.stderr


@pytest.mark.parametrize("contents", [b"", b"\n", b"aa\nbb", b"aa\rbb", b"x" * 4097, b"\xff"])
def test_invalid_seed_file_is_refused_without_environment_fallback(tmp_path, monkeypatch, contents):
    seed_file = tmp_path / "identity.seed"
    seed_file.write_bytes(contents)
    seed_file.chmod(0o600)
    monkeypatch.setenv("SIGN_SEED", SEED)
    out = run("did", "--seed-file", str(seed_file))
    assert out.returncode != 0
    assert out.stdout == ""
    assert "seed file" in out.stderr
    assert SEED not in out.stderr


def test_missing_seed_file_is_refused_without_environment_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGN_SEED", SEED)
    out = run("did", "--seed-file", str(tmp_path / "missing.seed"))
    assert out.returncode != 0
    assert out.stdout == ""
    assert "cannot read seed file" in out.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO and device semantics")
def test_nonregular_seed_files_are_refused_without_blocking(tmp_path):
    fifo = tmp_path / "seed.fifo"
    os.mkfifo(fifo, 0o600)
    for path in (str(fifo), os.devnull):
        out = subprocess.run(
            [sys.executable, str(SIGNER), "did", "--seed-file", path],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=5,
        )
        assert out.returncode != 0
        assert "not a regular file" in out.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_keygen_never_replaces_a_symlink_target(tmp_path):
    target = tmp_path / "existing.seed"
    target.write_text("keep this key", encoding="utf-8")
    link = tmp_path / "identity.seed"
    link.symlink_to(target)
    out = run("keygen", "--seed-file", str(link))
    assert out.returncode != 0
    assert target.read_text(encoding="utf-8") == "keep this key"
    assert link.is_symlink()


def test_seed_file_can_sign_notes_and_delegations(tmp_path):
    seed_file = tmp_path / "identity.seed"
    seed_file.write_text(SEED + "\n", encoding="utf-8")
    seed_file.chmod(0o600)
    note = run("set", "--seed-file", str(seed_file), "room-owners", "example", "9", "owner")
    assert note.returncode == 0, note.stderr
    root, sig = note.stdout.splitlines()
    didkey.verify(root, sig, "room-owners|example|9|owner")

    agent = run("did", "--seed", "bb" * 32).stdout.strip()
    out = run("delegate", "--seed-file", str(seed_file), agent, "r:example", "1", "10")
    assert out.returncode == 0, out.stderr
    record = next(line for line in out.stdout.splitlines() if line.startswith("delegate:"))
    _, actual_agent, scope, expires, nonce, signature = record.split()
    assert actual_agent == agent and scope == "r:example" and nonce == "10"
    didkey.verify(root, signature, f"delegate|{root}|{agent}|{scope}|{expires}|{nonce}")


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
