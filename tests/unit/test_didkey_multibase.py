"""A multibase tag must not give one signing key a second accepted DID."""

import _client
import pytest
from _client import _keypair, _post_signed, _say_signed

client = _client.client


@pytest.mark.parametrize("send", [_say_signed, _post_signed], ids=["get", "post"])
def test_signed_writes_reject_a_wrong_multibase_tag(client, send):
    # The length still matches. Only public_key's tag check refuses this alias;
    # changing its length-or-tag guard to `and` otherwise accepts the same key.
    did, sign = _keypair()
    alias = did.replace("did:key:z", "did:key:m", 1)
    assert len(alias) == len(did)

    refused = send(client, "multibase-tag", alias, sign, "signed content", nonce=1)
    assert refused.status_code == 400
    assert "multibase" in refused.text
    assert client.get("/r/multibase-tag?format=json").json()["messages"] == []

    # Same key, payload and nonce: the encoding refusal must leave the valid write usable.
    assert send(client, "multibase-tag", did, sign, "signed content", nonce=1).status_code == 200
    records = client.get("/r/multibase-tag?format=json").json()["messages"]
    assert len(records) == 1
    assert records[0]["from"] == did
    assert records[0]["nonce"] == 1
