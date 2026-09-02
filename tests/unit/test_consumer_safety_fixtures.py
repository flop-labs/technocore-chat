"""Keep the language-neutral consumer-safety corpus internally consistent."""

from __future__ import annotations

import json
import re
from pathlib import Path

import orjson
import pytest

import didkey
import store

FIXTURE = Path(__file__).parents[1] / "fixtures" / "consumer_safety_v1.json"

THREATS = {
    "signed_malicious_instruction",
    "unsigned_impersonation",
    "false_authority_claim",
    "prior_generation_record",
    "server_looking_metadata",
    "side_effect_url",
    "signed_tuple_replay",
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
REPLAY_RESULTS = {"first_seen", "duplicate_signed_tuple", "unknown", "not_applicable"}
URL_RESULTS = {"none", "potential_side_effect"}
STORED_REQUIRED_FIELDS = {"seq", "ts", "from", "text"}
STORED_OPTIONAL_FIELDS = {"nonce", "sig"}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_corpus_covers_the_adversarial_consumer_boundary() -> None:
    corpus = _load()

    assert corpus["schema_version"] == 1
    assert corpus["signature_input"] == "<room>|<nonce>|<text>"
    assert "never grants execution authority" in corpus["invariant"]
    history = corpus["room_history"]
    assert history == {
        "room": "consumer-room",
        "previous_generation": 2,
        "previous_generation_last_seq": 40,
        "current_generation": 3,
        "current_generation_first_seq": 41,
    }
    assert history["current_generation_first_seq"] > history["previous_generation_last_seq"]
    replay_scenario = corpus["same_generation_replay_scenario"]
    assert replay_scenario["first_observation_case_id"] == "signed-side-effect-url"
    assert replay_scenario["replay_case_id"] == "same-generation-signed-tuple-replay"
    assert "READ_BUDGET" in replay_scenario["acceptance_precondition"]

    cases = corpus["cases"]
    assert {case["threat"] for case in cases} == THREATS
    assert len({case["id"] for case in cases}) == len(cases)

    for case in cases:
        record = case["record"]
        context = case["consumer_context"]
        expected = case["expected"]

        assert case["room"] == history["room"]
        assert set(record).issubset(STORED_REQUIRED_FIELDS | STORED_OPTIONAL_FIELDS)
        assert STORED_REQUIRED_FIELDS.issubset(record)
        assert TIMESTAMP.fullmatch(record["ts"])
        assert set(context) == {
            "current_generation",
            "room_topic",
            "prior_observed_case_ids",
        }
        assert set(expected) == {
            "signature",
            "identity_evidence",
            "authority",
            "freshness",
            "replay",
            "url_risk",
            "automatic_action",
        }
        assert expected["signature"] in SIGNATURE_RESULTS
        assert expected["identity_evidence"] in IDENTITY_RESULTS
        assert expected["freshness"] in FRESHNESS_RESULTS
        assert expected["replay"] in REPLAY_RESULTS
        assert expected["url_risk"] in URL_RESULTS
        assert expected["authority"] == "none"
        assert expected["automatic_action"] is False

        if case["generation"] < context["current_generation"]:
            assert expected["freshness"] == "prior_generation"
        else:
            assert case["generation"] == context["current_generation"]
            assert expected["freshness"] == "current_generation"

        has_url = "https://" in record["text"]
        assert (expected["url_risk"] == "potential_side_effect") is has_url


def test_replay_classification_requires_an_ordered_prior_observation() -> None:
    cases = _load()["cases"]
    by_id = {case["id"]: case for case in cases}

    for index, case in enumerate(cases):
        prior_ids = case["consumer_context"]["prior_observed_case_ids"]
        assert all(prior_id in by_id for prior_id in prior_ids)
        assert all(cases.index(by_id[prior_id]) < index for prior_id in prior_ids)

        replay = case["expected"]["replay"]
        record = case["record"]
        if replay == "duplicate_signed_tuple":
            assert len(prior_ids) == 1
            prior = by_id[prior_ids[0]]
            assert prior["room"] == case["room"]
            assert prior["generation"] == case["generation"]
            assert prior["record"]["seq"] != record["seq"]
            assert prior["record"]["seq"] + 1 < record["seq"]
            assert prior["record"]["ts"] != record["ts"]
            signed_fields = ("from", "text", "nonce", "sig")
            assert all(prior["record"][field] == record[field] for field in signed_fields)
        elif replay == "first_seen":
            assert not prior_ids
            assert "sig" in record


def test_signature_classifications_match_the_fixture_bytes() -> None:
    for case in _load()["cases"]:
        record = case["record"]
        status = case["expected"]["signature"]
        source = record["from"]
        signature = record.get("sig")
        nonce = record.get("nonce")

        if status == "absent":
            assert signature is None and nonce is None and not didkey.is_did(source)
            continue

        assert didkey.is_did(source)
        if status == "not_reverifiable":
            assert signature is None and isinstance(nonce, int)
            continue

        assert isinstance(signature, str) and isinstance(nonce, int)
        signed = f"{case['room']}|{nonce}|{record['text']}"
        if status == "valid":
            didkey.verify(source, signature, signed)
        else:
            assert status == "invalid"
            with pytest.raises(didkey.SignatureError):
                didkey.verify(source, signature, signed)


def test_same_generation_replay_can_land_after_the_nonce_scan_forgets(tmp_path: Path) -> None:
    corpus = _load()
    by_id = {case["id"]: case for case in corpus["cases"]}
    scenario = corpus["same_generation_replay_scenario"]
    first = by_id[scenario["first_observation_case_id"]]
    replay = by_id[scenario["replay_case_id"]]
    record = first["record"]

    stored_first = store.append(
        tmp_path,
        first["room"],
        "",
        record["text"],
        did=record["from"],
        nonce=record["nonce"],
        sig=record["sig"],
    )
    room_path = store.room_path(tmp_path, first["room"])
    original = room_path.read_bytes()

    seq = stored_first["seq"] + 1
    with room_path.open("ab") as room_file:
        newer_bytes = 0
        while newer_bytes <= store.READ_BUDGET:
            line = (
                orjson.dumps(
                    {
                        "seq": seq,
                        "ts": store._now(),
                        "from": "bury",
                        "text": "x" * 200,
                    }
                )
                + b"\n"
            )
            room_file.write(line)
            newer_bytes += len(line)
            seq += 1

    stored_replay = store.append(
        tmp_path,
        replay["room"],
        "",
        replay["record"]["text"],
        did=replay["record"]["from"],
        nonce=replay["record"]["nonce"],
        sig=replay["record"]["sig"],
    )

    assert room_path.read_bytes().startswith(original)
    assert stored_replay["seq"] > stored_first["seq"]
    assert stored_replay["ts"] != stored_first["ts"]
    for field in ("from", "text", "nonce", "sig"):
        assert stored_replay[field] == stored_first[field]
