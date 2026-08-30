"""Keep the language-neutral consumer-safety corpus internally consistent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import didkey

FIXTURE = Path(__file__).parents[1] / "fixtures" / "consumer_safety_v1.json"

THREATS = {
    "signed_malicious_instruction",
    "unsigned_impersonation",
    "false_authority_claim",
    "prior_generation_replay",
    "server_looking_metadata",
    "side_effect_url",
    "invalid_signature",
    "missing_retained_signature",
}
SIGNATURE_RESULTS = {"valid", "invalid", "absent", "not_reverifiable"}
IDENTITY_RESULTS = {
    "verified_did_key",
    "unverified_claim",
    "unverified_did_claim",
    "invalid",
}
FRESHNESS_RESULTS = {"current_generation", "prior_generation"}
URL_RESULTS = {"none", "potential_side_effect"}


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_corpus_covers_the_adversarial_consumer_boundary() -> None:
    corpus = _load()

    assert corpus["schema_version"] == 1
    assert corpus["signature_input"] == "<room>|<nonce>|<text>"
    assert "never grants execution authority" in corpus["invariant"]

    cases = corpus["cases"]
    assert {case["threat"] for case in cases} == THREATS
    assert len({case["id"] for case in cases}) == len(cases)

    for case in cases:
        record = case["record"]
        context = case["consumer_context"]
        expected = case["expected"]

        assert set(record) == {"room", "room_epoch", "seq", "from", "text", "nonce", "sig"}
        assert set(context) == {"current_room_epoch", "room_topic"}
        assert set(expected) == {
            "signature",
            "identity_evidence",
            "authority",
            "freshness",
            "url_risk",
            "automatic_action",
        }
        assert expected["signature"] in SIGNATURE_RESULTS
        assert expected["identity_evidence"] in IDENTITY_RESULTS
        assert expected["freshness"] in FRESHNESS_RESULTS
        assert expected["url_risk"] in URL_RESULTS
        assert expected["authority"] == "none"
        assert expected["automatic_action"] is False

        if record["room_epoch"] < context["current_room_epoch"]:
            assert expected["freshness"] == "prior_generation"
        else:
            assert record["room_epoch"] == context["current_room_epoch"]
            assert expected["freshness"] == "current_generation"

        has_url = "https://" in record["text"]
        assert (expected["url_risk"] == "potential_side_effect") is has_url


def test_signature_classifications_match_the_fixture_bytes() -> None:
    for case in _load()["cases"]:
        record = case["record"]
        status = case["expected"]["signature"]
        source = record["from"]
        signature = record["sig"]
        nonce = record["nonce"]

        if status == "absent":
            assert signature is None and nonce is None and not didkey.is_did(source)
            continue

        assert didkey.is_did(source)
        if status == "not_reverifiable":
            assert signature is None and isinstance(nonce, int)
            continue

        assert isinstance(signature, str) and isinstance(nonce, int)
        signed = f"{record['room']}|{nonce}|{record['text']}"
        if status == "valid":
            didkey.verify(source, signature, signed)
        else:
            assert status == "invalid"
            with pytest.raises(didkey.SignatureError):
                didkey.verify(source, signature, signed)
