"""The Node example must remain wire-compatible with the Python/server contract."""

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


def run_node(*args: str) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is not installed")
    return subprocess.run(
        [NODE, str(NODE_SIGNER), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SIGN_SEED": SEED},
    )


def run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PYTHON_SIGNER), args[0], "--seed", SEED, *args[1:]],
        capture_output=True,
        text=True,
        cwd=ROOT,
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
