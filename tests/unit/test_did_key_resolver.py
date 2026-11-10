import json
import os
import subprocess
import sys

import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from contrib.did_key_resolver import DidResolutionError, resolve
from didkey import public_key

EXAMPLE = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def did_for(encoded: bytes) -> str:
    n = int.from_bytes(encoded, "big")
    digits = ""
    while n:
        n, remainder = divmod(n, 58)
        digits = ALPHABET[remainder] + digits
    leading = len(encoded) - len(encoded.lstrip(b"\x00"))
    return "did:key:z" + "1" * leading + digits


def test_document_matches_published_example():
    document = resolve(EXAMPLE)
    fingerprint = EXAMPLE.removeprefix("did:key:")
    reference = f"{EXAMPLE}#{fingerprint}"
    assert document["@context"] == [
        "https://www.w3.org/ns/did/v1",
        "https://w3id.org/security/multikey/v1",
    ]
    assert document["id"] == EXAMPLE
    assert document["verificationMethod"] == [
        {
            "id": reference,
            "type": "Multikey",
            "controller": EXAMPLE,
            "publicKeyMultibase": fingerprint,
        }
    ]
    for relationship in (
        "authentication",
        "assertionMethod",
        "capabilityInvocation",
        "capabilityDelegation",
    ):
        assert document[relationship] == [reference]
    assert "keyAgreement" not in document
    assert resolve(EXAMPLE) == document


def test_resolver_never_opens_a_network_socket(monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("resolution must not use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    assert resolve(EXAMPLE)["id"] == EXAMPLE


@pytest.mark.parametrize("seed", [bytes([n]) * 32 for n in (1, 2, 3)])
def test_generated_ed25519_keys_round_trip(seed):
    key = bytes(SigningKey(seed).verify_key)
    did = did_for(b"\xed\x01" + key)
    assert public_key(did) == key
    assert resolve(did)["verificationMethod"][0]["publicKeyMultibase"] == did[8:]


def test_resolved_key_verifies_original_but_not_tampered_message():
    signer = SigningKey(bytes([7]) * 32)
    did = did_for(b"\xed\x01" + bytes(signer.verify_key))
    method = resolve(did)["verificationMethod"][0]
    verifier = VerifyKey(public_key("did:key:" + method["publicKeyMultibase"]))
    message = b"kibble|123|hello"
    signature = signer.sign(message).signature
    verifier.verify(message, signature)
    with pytest.raises(BadSignatureError):
        verifier.verify(b"kibble|123|changed", signature)


@pytest.mark.parametrize(
    "identifier",
    [
        None,
        123,
        "",
        "did:web:example.com",
        EXAMPLE + "#key",
        EXAMPLE + "?query=1",
        EXAMPLE + "/path",
        EXAMPLE[:-1],
        EXAMPLE[:-1] + "0",
        did_for(b"\xec\x01" + b"\x01" * 32),
        did_for(b"\xed\x01" + b"\x00" * 32),
    ],
)
def test_invalid_or_unsupported_identifiers_fail_closed(identifier):
    with pytest.raises(DidResolutionError):
        resolve(identifier)


def test_cli_outputs_json_and_reports_errors():
    command = [sys.executable, "-m", "contrib.did_key_resolver"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(["src", os.getcwd()])}
    good = subprocess.run([*command, EXAMPLE], capture_output=True, text=True, env=env, check=True)
    assert json.loads(good.stdout) == resolve(EXAMPLE)
    bad = subprocess.run([*command, EXAMPLE + "#key"], capture_output=True, text=True, env=env)
    assert bad.returncode != 0
    assert bad.stdout == ""
    assert "invalid or unsupported" in bad.stderr
