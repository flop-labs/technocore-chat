"""A published DID shape is necessary, but decoded codec validation still matters."""

import re

import pytest

import didkey


def test_shape_match_does_not_establish_ed25519_multicodec():
    shaped_non_key = "did:key:z6Mk" + "1" * 44
    assert re.fullmatch(didkey.DID_PATTERN, shaped_non_key)
    assert len(shaped_non_key) == len(didkey.PREFIX) + didkey.MULTIBASE_CHARS
    with pytest.raises(didkey.DidError, match="only ed25519-pub"):
        didkey.public_key(shaped_non_key)
    assert not didkey.is_did(shaped_non_key)
