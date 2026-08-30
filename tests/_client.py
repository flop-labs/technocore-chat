"""Run: uv run --group dev python -m pytest tests"""

import os
import time
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """One shared app, a fresh ROOT per test: config.override re-binds the knob (and app's
    copy of it) for exactly this test, where the old fixture re-imported app against a
    CHAT_ROOT env var. The limiter buckets and the three memo caches behind /rooms are
    process state a fresh import used to reset for free, so they are cleared here instead.

    Their clock is pinned here too. Cache validity is part of the cache key now (see
    store._time_bucket), so the boundary between one window and the next is a key change —
    and unpinned those boundaries are cut on a free-running clock, which lands one inside
    roughly one test in every ROOMS_CACHE_SECONDS of suite time. A test that pins a 60s
    window "far above anything it spends" would still miss on those, for a reason no test
    body could name. Anchoring the buckets to the moment the fixture ran puts a fast test
    inside bucket 0 for the whole of it, and leaves a test that wants a window to pass
    saying so out loud."""
    import app as app_module
    import config
    import limit
    import store

    origin = time.monotonic()
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int((now - origin) // ttl))
    app_module._buckets.clear()
    app_module._rooms_walk.cache_clear()
    store._cached_window.cache_clear()
    store._topics_memo.cache_clear()
    app_module._identities.clear()
    app_module._proxy_evidence["proxied_requests"] = 0
    # The duplicate ring is the same kind of process state the buckets are: a fresh
    # import used to reset it for free. Cleared whether or not a test enables the
    # filter, so a phrase posted in one test cannot make the next test's identical
    # phrase arrive as the fourth copy of something.
    limit._dupes.clear()
    # The cross-sender filter is pinned OFF here so a test that is not about the filter
    # never rides on its shipped default: a suite whose rooms all behave pre-filter is
    # hermetic against any future retune of DUPE_* (they moved once already, 0/3 -> 60/5,
    # and tests that silently depended on the old defaults were the debris). The tests
    # that exercise the filter configure it explicitly; the defaults themselves are
    # asserted once, by the boot probe in tests/unit/test_config_knobs.py.
    with config.override(ROOT=tmp_path, DUPE_FILTER_SECONDS=0):
        yield TestClient(app_module.app)


# --------------------------------------------------------------------------- shared helpers
#
# Four things every lifecycle test needs, written once. The reaper, the ring and the
# signed lane are all clock- and race-sensitive, and open-coding that at 40-odd call sites
# buried the one line of each test that was actually the point.


def _age(path, seconds):
    """Move a file `seconds` into the past.

    The reaper stats mtime, so this is how a test says "nobody has touched this for a
    week" without waiting one. Callers pass the threshold plus a margin —
    `_age(p, store.IDLE_SECONDS + 60)` — which reads as the rule it is testing.
    """
    when = time.time() - seconds
    os.utime(path, (when, when))


# -------------------------------------------------- engagement tripwires (analysis §II.2.2)


def _stats_for(tmp_path, room):
    import store

    return {r["room"]: r for r in store.room_stats(tmp_path)["rooms"]}[room]


# ------------------------------------------------------------ signed writes (did:key)


def _multibase(raw: bytes) -> str:
    """base58btc, the encoding a `did:key` multibase segment is written in.

    Spelt out rather than imported: `didkey` only ever decodes, and a test that built its
    keys with the decoder's own inverse could not catch the decoder being wrong.
    """
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    return out


def _keypair(seed: int = 1):
    """A deterministic Ed25519 key and its did:key, so a failure is reproducible."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import didkey

    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    raw = key.public_key().public_bytes_raw()

    def sign(message: str) -> str:
        return base64.urlsafe_b64encode(key.sign(message.encode())).decode().rstrip("=")

    return f"{didkey.PREFIX}z{_multibase(didkey.MULTICODEC_ED25519 + raw)}", sign


def _say_signed(client, room, did, sign, text, nonce=1):
    """The canonical string is `room|nonce|text` over the *swept* text — what is stored."""
    import store

    body = store.clean_text(text)
    return client.get(f"/r/{room}/say-signed/{did}/{sign(f'{room}|{nonce}|{body}')}/{nonce}/{text}")


def _post_signed(client, room, did, sign, text, nonce=1):
    """POST the same signed message as `_say_signed`, including the pre-storage sweep."""
    import store

    body = store.clean_text(text)
    return client.post(
        f"/r/{room}",
        json={"did": did, "sig": sign(f"{room}|{nonce}|{body}"), "nonce": str(nonce), "text": text},
    )


# ---------------------------------------------------------------------- owned rooms


def _claim(client, room, did, sign, nonce=1):
    """A claim is a signed write storing the signer's own key. The nonce counter is per
    room and shared with room-allow, so claiming burns 1 and allow-list writes start at 2."""
    return _set_signed(client, "room-owners", room, did, sign, did, nonce)


def _set_signed(client, ns, key, did, sign, value, nonce=1):
    return client.get(
        f"/kv/{ns}/{key}/set-signed/{did}/{sign(f'{ns}|{key}|{nonce}|{value}')}/{nonce}/{value}"
    )


# ------------------------------------------------------------------ ephemeral rooms


def _at(monkeypatch, store, stamp):
    monkeypatch.setattr(store, "_now", lambda: stamp)


def _race_before_lock(monkeypatch, store, path, action):
    """Run `action()` once, in the gap between the store reading a file and locking it.

    Every race worth testing here lives in that gap: the store reads, decides, then takes
    the lock and writes. A second writer landing in between is what the compare-and-set
    and the reaper's under-lock recheck exist to survive, and this puts one there without
    threads. Returns a list that is non-empty once the race has actually happened — assert
    on it, or a test that stopped reaching the gap will pass while proving nothing.
    """
    real_locked = store._locked
    fired = []

    @contextmanager
    def hook(target):
        if target == path and not fired:
            fired.append(True)
            action()
        with real_locked(target):
            yield

    monkeypatch.setattr(store, "_locked", hook)
    return fired
