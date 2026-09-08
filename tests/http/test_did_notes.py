"""Run: uv run --group dev python -m pytest tests"""

import hashlib

import _client
from _client import _keypair, _multibase, _set_signed

client = _client.client  # the shared TestClient fixture


def _fingerprint(did: str) -> str:
    """The manual's IDENTITY rule: first 16 lowercase hex of SHA-256 of the did:key."""
    return hashlib.sha256(did.encode()).hexdigest()[:16]


def test_an_identity_slot_accepts_the_key_that_names_it(client):
    """The legacy namespace keeps working for a correct write: #199 refuses misfiles, it
    does not push migration — readers still fall back to /kv/did/<fingerprint>."""
    did, _ = _keypair(31)
    fp = _fingerprint(did)
    assert client.get(f"/kv/did/{fp}/set/{did}").status_code == 200
    assert did in client.get(f"/kv/did/{fp}").text


def test_a_sharded_identity_slot_accepts_the_key_that_names_it(client):
    did, _ = _keypair(32)
    fp = _fingerprint(did)
    assert client.get(f"/kv/did-{fp[:2]}/{fp[2:]}/set/{did}").status_code == 200
    assert did in client.get(f"/kv/did-{fp[:2]}/{fp[2:]}").text


def test_extra_material_after_the_did_stays_legal(client):
    """A DID note is more than the key: patterns.md publishes x25519 and mailbox material
    after it, and only the first did:key token is held to the slot."""
    did, _ = _keypair(33)
    fp = _fingerprint(did)
    value = f"{did}%20x25519:aGVsbG9oZWxsbw%20mailbox:mb-p-quiet"
    assert client.get(f"/kv/did-{fp[:2]}/{fp[2:]}/set/{value}").status_code == 200
    assert "mailbox:mb-p-quiet" in client.get(f"/kv/did-{fp[:2]}/{fp[2:]}").text


def test_a_wrong_slot_write_is_refused_and_names_the_right_slot(client):
    """Issue #199's largest class: a well-formed key filed where no lookup will ever find
    it. The refusal must hand back the address that would work."""
    did, _ = _keypair(34)
    other, _ = _keypair(35)
    fp, wrong = _fingerprint(did), _fingerprint(other)
    r = client.get(f"/kv/did/{wrong}/set/{did}")
    assert r.status_code == 400
    assert f"fingerprints to {fp}" in r.text
    assert f"/kv/did-{fp[:2]}/{fp[2:]}" in r.text
    assert client.get(f"/kv/did/{wrong}").status_code == 404  # nothing was stored
    r = client.get(f"/kv/did-{wrong[:2]}/{wrong[2:]}/set/{did}")
    assert r.status_code == 400 and f"fingerprints to {fp}" in r.text


def test_a_key_that_is_not_ed25519_pub_is_refused(client):
    """#199 found five zc4T…/zc4U… values: 38 bytes behind a leading 0xed01, so a prefix
    check alone passes them and only the length gives them away. The refusal surfaces the
    parser's own diagnosis — the caller learns what is wrong, not just that something is."""
    import didkey

    fake = f"{didkey.PREFIX}z{_multibase(didkey.MULTICODEC_ED25519 + bytes([7]) * 36)}"
    assert fake[len(didkey.PREFIX) :].startswith("zc4")  # the shape the issue sampled
    r = client.get(f"/kv/did/{_fingerprint(fake)}/set/{fake}")
    assert r.status_code == 400
    assert "expected 48 multibase characters" in r.text


def test_a_key_glued_to_its_extra_material_is_told_about_the_space(client):
    """The likeliest publishing typo: the %20 between the key and `x25519:` dropped, so
    the greedy token runs long. The diagnosis names the length, and the correction names
    the separator — not a demand to publish the key the caller already published."""
    did, _ = _keypair(39)
    r = client.get(f"/kv/did/{_fingerprint(did)}/set/{did}x25519aaaa")
    assert r.status_code == 400
    assert "multibase characters" in r.text
    assert "Separate material after the key" in r.text


def test_a_value_with_no_did_key_is_refused(client):
    """#199's third class: session state misfiled into the shared identity namespace. The
    refusal names the fingerprint the slot wants — reassembled shard+key on the sharded
    path — and points at a namespace of the writer's own."""
    r = client.get("/kv/did/0000000000000000/set/agent:xiuxiu-073%20active")
    assert r.status_code == 400
    assert "is an identity slot" in r.text
    assert "sha256[:16] is 0000000000000000" in r.text
    assert "/kv/p-<random>/state" in r.text
    sharded = client.get(f"/kv/did-ab/{'c' * 14}/set/agent-state")
    assert sharded.status_code == 400
    assert f"sha256[:16] is ab{'c' * 14}" in sharded.text


def test_the_first_did_key_token_is_the_one_held_to_the_slot(client):
    """The measurement's extraction rule, both halves: leading non-key material is legal
    (the token may sit anywhere), and the FIRST did:key token is the identity — a later
    one cannot rescue the write."""
    first, _ = _keypair(40)
    second, _ = _keypair(41)
    fp = _fingerprint(first)
    value = f"x25519:aGVsbG8%20{first}%20{second}"
    assert client.get(f"/kv/did/{fp}/set/{value}").status_code == 200
    r = client.get(f"/kv/did/{_fingerprint(second)}/set/{value}")
    assert r.status_code == 400 and f"fingerprints to {fp}" in r.text


def test_a_slot_that_cannot_be_a_fingerprint_says_so(client):
    """A well-formed key that no SHA-256 prefix can equal gets the truth, not an
    instruction to produce an impossible did:key; a malformed key keeps the name rule's
    own diagnosis, same as every other namespace."""
    r = client.get("/kv/did/alice/set/agent-state")
    assert r.status_code == 400 and "cannot name an identity" in r.text
    r = client.get(f"/kv/did-ab/{'c' * 12}/set/agent-state")
    assert r.status_code == 400 and "cannot name an identity" in r.text
    r = client.get("/kv/did/ALICE/set/agent-state")
    assert r.status_code == 400 and "bad name" in r.text


def test_a_refused_identity_write_burns_no_note_budget(client):
    """The gate refuses before note_set, so no create is reserved and none is counted —
    the property test_a_refused_write_counts_nothing pins at the store layer, asserted
    here on the note gauge /rooms publishes."""

    def notes_gauge() -> str:
        lines = client.get("/rooms").text.splitlines()
        return next(ln for ln in lines if ln.startswith("# notes "))

    did, _ = _keypair(36)
    fp = _fingerprint(did)
    assert client.get(f"/kv/did/{fp}/set/{did}").status_code == 200
    before = notes_gauge()
    assert client.get("/kv/did/aaaaaaaaaaaaaaaa/set/no-key-here").status_code == 400
    assert client.get(f"/kv/did/bbbbbbbbbbbbbbbb/set/{did}").status_code == 400
    assert client.get(f"/kv/did/cccccccccccccccc/set/{did}?if_absent=1").status_code == 400
    assert notes_gauge() == before, "a refused write moved the note gauge"
    listed = [ln for ln in client.get("/kv/did").text.splitlines() if ln.startswith("/kv/")]
    assert listed == [f"/kv/did/{fp}"], "a refused write left a note behind"


def test_the_identity_gate_runs_before_cas_and_leaves_cas_intact(client):
    """A refused write must not read the slot for its answer: 400, not 409, and no current
    value in the body. A valid write still meets the ordinary CAS contract afterwards."""
    did, _ = _keypair(37)
    fp = _fingerprint(did)
    assert client.get(f"/kv/did/{fp}/set/{did}").status_code == 200
    r = client.get(f"/kv/did/{fp}/set/junk-state?if={did}")
    assert r.status_code == 400 and did not in r.text
    r = client.get(f"/kv/did/{fp}/set/junk-state?if_absent=1")
    assert r.status_code == 400 and did not in r.text
    assert did in client.get(f"/kv/did/{fp}").text  # untouched
    stale = client.get(f"/kv/did/{fp}/set/{did}?if=stale")
    assert stale.status_code == 409 and did in stale.text  # CAS itself is unchanged


def test_the_post_lane_enforces_the_same_rule(client):
    did, _ = _keypair(38)
    fp = _fingerprint(did)
    r = client.post(f"/kv/did/{fp}", json={"value": "agent state, no key"})
    assert r.status_code == 400 and "is an identity slot" in r.text
    assert client.post(f"/kv/did/{fp}", json={"value": did}).status_code == 200


def test_namespaces_that_merely_resemble_the_identity_ones_stay_world_writable(client):
    """The gate is `did` and `did-<2 lowercase hex>` exactly — the shard alphabet the
    manual defines. Near-misses are ordinary namespaces and keep taking anything."""
    for ns in ("plans", "didx", "did-zz", "did-abc", "did-a", "diddly"):
        assert client.get(f"/kv/{ns}/anything/set/no-did-here").status_code == 200, ns


def test_the_signed_lane_still_refuses_identity_namespaces_before_the_gate(client):
    """Scope containment in the other direction: a signed write to `did` keeps the
    pre-existing signed-writes-are-ownership-only refusal, not a fingerprint error."""
    did, sign = _keypair(42)
    r = _set_signed(client, "did", _fingerprint(did), did, sign, did)
    assert r.status_code == 400
    assert "signed note writes are only accepted" in r.text


def test_both_note_write_lanes_document_the_identity_refusal(client):
    """The contract check only proves a 400 is documented; the description carrying the
    identity-slot cause on BOTH unsigned lanes is what a machine reader consults."""
    paths = client.get("/openapi.json").json()["paths"]
    for path, method in (("/kv/{ns}/{key}/set/{value}", "get"), ("/kv/{ns}/{key}", "post")):
        desc = paths[path][method]["responses"]["400"]["description"]
        assert "identity-slot mismatch" in desc, (path, desc)
