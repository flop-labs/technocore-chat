"""Interoperability tests for the documented contribution-proof-v1 format."""

import json
from importlib import util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPEC = util.spec_from_file_location(
    "verify_contribution_proof", ROOT / "scripts" / "verify_contribution_proof.py"
)
assert SPEC and SPEC.loader
verifier = util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


@pytest.fixture
def proof() -> dict[str, str]:
    return json.loads((ROOT / "tests/fixtures/contribution-proof-v1.json").read_text())


def test_published_vector_verifies(proof: dict[str, str]) -> None:
    verifier.verify(proof)


def test_canonical_payload_is_sorted_compact_utf8() -> None:
    assert verifier.canonical_payload("https://e.test/é", "a" * 40) == (
        b'{"artifact_url":"https://e.test/\xc3\xa9",'
        b'"commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"schema":"technocore-contribution-v1"}'
    )


def test_tampering_or_noncanonical_fields_is_rejected(proof: dict[str, str]) -> None:
    for field, value in (("artifact_url", "https://e.test/other"), ("commit", "A" + "a" * 39)):
        tampered = proof | {field: value}
        with pytest.raises(ValueError):
            verifier.verify(tampered)

    padded = proof | {"signature": proof["signature"] + "="}
    with pytest.raises(ValueError, match="unpadded"):
        verifier.verify(padded)


def test_wrong_schema_signature_is_rejected(proof: dict[str, str]) -> None:
    payload = verifier.canonical_payload(proof["artifact_url"], proof["commit"])
    assert b"technocore-contribution-v1" in payload
    altered = proof | {"signature": proof["signature"][:-1] + "A"}
    with pytest.raises(ValueError):
        verifier.verify(altered)
