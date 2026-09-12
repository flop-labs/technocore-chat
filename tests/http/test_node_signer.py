"""The Node example must remain wire-compatible with the Python/server contract.

Negative-input and parity coverage builds on Orshengnudor's PR #577 cases, while
keeping this example's deliberately narrower hex-seed-only interface.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import _client  # noqa: F401 (imported for the fixture alias below)
import pytest

client = _client.client  # the shared TestClient fixture

ROOT = Path(__file__).resolve().parents[2]
NODE_SIGNER = ROOT / "examples" / "sign_node.mjs"
PYTHON_SIGNER = ROOT / "scripts" / "sign.py"
NODE = shutil.which("node")
SEED = "aa" * 32


def run_node(*args: str, seed: str | None = SEED) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is not installed")
    env = {key: value for key, value in os.environ.items() if key != "SIGN_SEED"}
    if seed is not None:
        env["SIGN_SEED"] = seed
    return subprocess.run(
        [NODE, str(NODE_SIGNER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        cwd=ROOT,
        env=env,
    )


def run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PYTHON_SIGNER), args[0], "--seed", SEED, *args[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_node_signer_matches_python_and_the_server(client) -> None:
    # One reachable representative for every swept category in JavaScript, plus
    # trimming at both ends and an interior Zs character that must survive.
    raw = "\u0085a\u200bb\ue000c\u2028d\u2029e\u00a0f\u200b"
    clean = "a b c d e\u00a0f"

    node = run_node("say", "node-signer", "7", raw)
    python = run_python("say", "node-signer", "7", raw)
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout

    did, signature = node.stdout.splitlines()
    encoded = quote(raw, safe="")
    response = client.get(f"/r/node-signer/say-signed/{did}/{signature}/7/{encoded}")
    assert response.status_code == 200, response.text
    assert clean in response.text


def test_node_note_signer_matches_python_and_the_server(client) -> None:
    identity = run_node("did")
    assert identity.returncode == 0, identity.stderr
    did = identity.stdout.strip()
    namespace = "room-owners"
    key = "d-node-signer"

    node = run_node("set", namespace, key, "8", did)
    python = run_python("set", namespace, key, "8", did)
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout

    signed_did, signature = node.stdout.splitlines()
    assert signed_did == did
    encoded = quote(did, safe="")
    response = client.get(f"/kv/{namespace}/{key}/set-signed/{did}/{signature}/8/{encoded}")
    assert response.status_code == 200, response.text
    assert did in client.get(f"/kv/{namespace}/{key}").text


def test_node_note_signer_sweeps_unicode() -> None:
    # Arbitrary Unicode values exercise canonical bytes offline. The service's
    # signed namespaces accept DID payloads, covered by the ownership test above.
    raw = "\u0085caf\u00e9\u200b\U0001f680\ue000a\u2028b\u2029c\u00a0d\u200b"
    clean = "caf\u00e9 \U0001f680 a b c\u00a0d"
    node = run_node("set", "coord", "node-unicode", "9", raw)
    python = run_python("set", "coord", "node-unicode", "9", raw)
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout
    swept = run_python("set", "coord", "node-unicode", "9", clean)
    assert swept.returncode == 0, swept.stderr
    assert node.stdout == swept.stdout


@pytest.mark.parametrize("seed", [None, "", "a" * 63, "a" * 65, "g" * 64, "a passphrase"])
def test_node_signer_requires_an_explicit_hex_seed(seed: str | None) -> None:
    result = run_node("did", seed=seed)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "SIGN_SEED must contain exactly 64 hexadecimal characters" in result.stderr


def test_node_signer_accepts_uppercase_hex_seed() -> None:
    node = run_node("did", seed=SEED.upper())
    python = run_python("did")
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("keygen", "extra"),
        ("did", "extra"),
        ("say", "room", "1", "hello", "extra"),
        ("set", "coord", "key", "1", "hello", "extra"),
    ],
)
def test_node_signer_rejects_extra_arguments(args: tuple[str, ...]) -> None:
    result = run_node(*args)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "accepts no arguments" in result.stderr or "usage:" in result.stderr


@pytest.mark.parametrize("prefix", [("say", "room"), ("set", "coord", "key")])
@pytest.mark.parametrize("nonce", ["", "1" * 20, "-1", "1.0", "\u0661", "1\n"])
def test_node_signer_rejects_invalid_nonce(prefix: tuple[str, ...], nonce: str) -> None:
    result = run_node(*prefix, nonce, "hello")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "nonce must contain 1-19 ASCII digits" in result.stderr


@pytest.mark.parametrize("prefix", [("say", "room"), ("set", "coord", "key")])
@pytest.mark.parametrize("nonce", ["0", "0007", "9" * 19])
def test_node_signer_preserves_nonce_bytes(prefix: tuple[str, ...], nonce: str) -> None:
    node = run_node(*prefix, nonce, "hello")
    python = run_python(*prefix, nonce, "hello")
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout


@pytest.mark.parametrize("prefix", [("say", "room"), ("set", "coord", "key")])
def test_node_signer_refuses_empty_swept_payload(prefix: tuple[str, ...]) -> None:
    result = run_node(*prefix, "1", "\u0085\u200b\ue000\u2028\u2029 ")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "nothing visible remains" in result.stderr


@pytest.mark.parametrize(
    ("prefix", "cap"), [(("say", "room"), 4096), (("set", "coord", "key"), 8192)]
)
def test_node_signer_caps_count_code_points_after_sweeping(
    prefix: tuple[str, ...], cap: int
) -> None:
    # Astral characters distinguish code points from JS UTF-16 units; padding
    # distinguishes the swept length from the raw argument length.
    raw = "\u200b" + "\U0001f680" * cap + "\n"
    node = run_node(*prefix, "1", raw)
    python = run_python(*prefix, "1", raw)
    assert node.returncode == 0, node.stderr
    assert python.returncode == 0, python.stderr
    assert node.stdout == python.stdout

    rejected = run_node(*prefix, "1", "\U0001f680" * (cap + 1))
    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert f"over the {cap}-character cap" in rejected.stderr
