"""The local signer against the real server, not beside it.

PR #14 was closed because its client scripts were standalone: they had their own tests,
their own copy of the sweep, and nothing that ran them against the thing they had to
agree with. So every assertion here goes through the `client` fixture and the actual
app. The one that matters most is the sweep: a client that signs what the user typed is
correct on every ASCII string and refused on the first one carrying a zero-width space,
and that is the failure a standalone test cannot see.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import _client  # noqa: F401 (imported for the fixture alias below)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))

import didkey  # noqa: E402
from technocore_client import Keyring, NonceStore, Signer  # noqa: E402

client = _client.client


def say(client, signer: Signer, room: str, text: str):
    did, sig, nonce, swept = signer.message(room, text)
    return client.get(f"/r/{room}/say-signed/{did}/{sig}/{nonce}/{quote(swept)}"), swept


def test_a_local_signature_is_accepted_by_the_real_server(client, tmp_path) -> None:
    signer = Signer(tmp_path / "home")
    response, swept = say(client, signer, "localsigner", "hello from the local signer")
    assert response.status_code == 200, response.text
    assert swept in response.text
    assert "<z6Mk" in response.text  # rendered as the key, not a self-asserted nick


def test_the_signature_covers_the_swept_text_and_not_the_input(client, tmp_path) -> None:
    """The design's sharpest edge, end to end.

    U+200B is category Cf, so the server stores `a b` and verifies against that. Signing
    what the caller typed produces a signature over `a​b`, which is a string the
    server never sees — a 403 whose cause is invisible in the input.
    """
    signer = Signer(tmp_path / "home")
    raw = "a​b"

    did, sig, nonce, swept = signer.message("sweeproom", raw)
    assert swept == "a b"
    good = client.get(f"/r/sweeproom/say-signed/{did}/{sig}/{nonce}/{quote(swept)}")
    assert good.status_code == 200, good.text

    # The same write, signed over the raw input instead. Nothing else differs.
    bad_nonce = signer.nonces.allocate(signer.did, "sweeproom")
    bad_sig = signer.keys.sign(f"sweeproom|{bad_nonce}|{raw}")
    bad = client.get(f"/r/sweeproom/say-signed/{did}/{bad_sig}/{bad_nonce}/{quote(swept)}")
    assert bad.status_code == 403, bad.text


def test_the_server_verifies_what_the_signer_produced(client, tmp_path) -> None:
    signer = Signer(tmp_path / "home")
    response, swept = say(client, signer, "storedsig", "41 rooms at 20:31Z")
    assert response.status_code == 200

    record = client.get("/r/storedsig?format=json").json()["messages"][-1]
    didkey.verify(signer.did, record["sig"], f"storedsig|{record['nonce']}|{record['text']}")


def test_a_signed_note_write_is_accepted(client, tmp_path) -> None:
    """The signed note lane is narrower than "notes": the server takes a signed write only
    for `room-owners` and `room-allow`, because every other namespace is world-writable and
    a signature there would assert something the lane does not enforce. So the test claims
    a room, which is what the lane is for."""
    signer = Signer(tmp_path / "home")
    did, sig, nonce, swept = signer.note("room-owners", "d-localsigner", signer.did)
    r = client.get(
        f"/kv/room-owners/d-localsigner/set-signed/{did}/{sig}/{nonce}/{quote(swept)}"
        "?if_absent=1"
    )
    assert r.status_code == 200, r.text
    # And the claim is real: the unsigned lane is now refused on that room.
    assert client.get("/r/d-localsigner/say/stranger/hello").status_code == 403


def test_every_nonce_the_signer_mints_is_one_the_server_accepts(tmp_path) -> None:
    """The client's grammar must be a subset of the server's, not merely similar."""
    signer = Signer(tmp_path / "home")
    for i in range(5):
        nonce = signer.nonces.allocate(signer.did, f"room{i}")
        assert didkey.NONCE_RE.fullmatch(str(nonce)), nonce


def test_each_room_keeps_its_own_counter(tmp_path) -> None:
    """Scope is (key, room). Writing to one room must not consume another's number.

    Allocation is also monotonic across the whole process, which is allowed and not the
    same thing: the server's rule is a floor per room, so a nonce higher than that room
    needed is accepted. What would be wrong is a write to room `a` advancing room `b`'s
    stored counter, because then `b`'s next write would have to clear a bar nothing in
    room `b` ever set.
    """
    signer = Signer(tmp_path / "home")
    first_a = signer.nonces.allocate(signer.did, "a")
    first_b = signer.nonces.allocate(signer.did, "b")
    second_a = signer.nonces.allocate(signer.did, "a")
    assert second_a > first_a
    assert signer.nonces.last(signer.did, "b") == first_b


def test_a_nonce_handed_out_before_a_crash_is_never_handed_out_again(tmp_path) -> None:
    """The property the persistence exists for.

    The store is written before the number is returned, so a process that dies between
    being handed a nonce and using it loses the number rather than repeating it. This
    simulates the crash by dropping the object entirely and rebuilding from disk.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    did = signer.did
    lost = signer.nonces.allocate(did, "crashroom")
    del signer  # the process dies here; the write was never sent

    revived = NonceStore(home / "nonces.json")
    assert revived.last(did, "crashroom") == lost
    assert revived.allocate(did, "crashroom") > lost


def test_a_lost_nonce_file_does_not_restart_the_counter(tmp_path) -> None:
    """Losing the file degrades to the clock, not to 1.

    A counter that restarted would be refused by the server for as long as it took to
    climb back past what the key had already used, with nothing local to explain it.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    used = signer.nonces.allocate(signer.did, "room")
    (home / "nonces.json").unlink()

    fresh = NonceStore(home / "nonces.json")
    assert fresh.last(signer.did, "room") is None
    assert fresh.allocate(signer.did, "room") > used


def test_a_corrupt_nonce_file_is_not_fatal(tmp_path) -> None:
    home = tmp_path / "home"
    signer = Signer(home)
    used = signer.nonces.allocate(signer.did, "room")
    (home / "nonces.json").write_text("{ this is not json")

    fresh = NonceStore(home / "nonces.json")
    assert fresh.allocate(signer.did, "room") > used


def test_a_new_seed_makes_its_directory_entry_durable(tmp_path, monkeypatch) -> None:
    """@yukkie3276, #803: fsyncing the seed file does not make its *name* durable.

    Power loss is not reproducible in a test, so this asserts the call rather than the outcome —
    that a directory handle is fsynced after the seed is written, and that the directories the
    keyring itself created are covered too. Asserting the mechanism is weaker than asserting the
    property and is the strongest thing available here; the alternative is asserting nothing,
    which is how the gap survived review-free for a day.
    """
    home = tmp_path / "deep" / "nested" / "home"
    # Identify each fsynced handle by (device, inode) rather than by a path: /dev/fd does not
    # resolve back to a name on macOS, and an inode pair is exact on every platform this runs on.
    synced: set[tuple[int, int]] = set()
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.add((info.st_dev, info.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    signer = Signer(home)
    assert (home / "seed").exists()

    def ident(path: Path) -> tuple[int, int]:
        info = path.stat()
        return (info.st_dev, info.st_ino)

    # The directory holding the seed, and every directory this call had to create beneath an
    # existing one, must have had its own entries flushed.
    assert ident(home) in synced, "the seed's own directory was never fsynced"
    for created in (tmp_path / "deep", tmp_path / "deep" / "nested"):
        assert ident(created) in synced, f"{created.name} was created but its entry never flushed"
    # And the identity is stable across a reload, which is what the durability protects.
    assert Keyring(home / "seed").did == signer.did


def test_concurrent_first_starts_converge_on_one_identity(tmp_path) -> None:
    """@yukkie3276, #803: first start raced, and one starter lost for no good reason.

    `_load` checked `exists()` and then `_create` claimed the name with `O_EXCL`, so two
    processes starting against the same absent seed both passed the check and the loser raised
    instead of reading the winner's key. This package already promises a signer shared across
    concurrent processes for nonce allocation; initialisation has to converge too, or the
    promise only holds after somebody has started alone once.

    Six processes, one absent path, no coordination: all must succeed and all must report the
    same DID.
    """
    home = tmp_path / "home"
    gate = tmp_path / "go"
    # A start gate, because without one this test passed against the broken code roughly one run
    # in five: the children finished staggered and the race simply did not happen. A flaky guard
    # over a race is worse than none — it is a regression that goes green often enough to be
    # believed. Each child imports, then spins until the gate appears, so they all reach the
    # create within the same few microseconds.
    program = (
        "import os, sys, time;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Keyring;"
        "deadline = time.time() + 30;"
        "[time.sleep(0.001) for _ in iter(lambda: os.path.exists(sys.argv[3]) or time.time() > deadline, True)];"
        "print(Keyring(sys.argv[2]).did)"
    )
    repo = Path(__file__).resolve().parents[2]
    paths = f"{repo / 'client'}:{repo / 'src'}"
    procs = [
        subprocess.Popen([sys.executable, "-c", program, paths, str(home / "seed"), str(gate)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    time.sleep(0.5)  # let every child get past its imports and onto the gate
    gate.write_text("go")
    results = [SimpleNamespace(returncode=p.wait(timeout=60), **dict(zip(("stdout", "stderr"), p.communicate())))
               for p in procs]

    failed = [r.stderr.strip().splitlines()[-1] for r in results if r.returncode != 0]
    assert not failed, f"a concurrent first start failed: {failed}"
    dids = {r.stdout.strip() for r in results}
    assert len(dids) == 1, f"first starts disagreed about the identity: {dids}"
    assert next(iter(dids)).startswith("did:key:z6Mk")
    # No temporary survived the race, and the winner's file carries the mode the loser skipped.
    assert list(home.glob("seed.*.tmp")) == []
    assert (home / "seed").stat().st_mode & 0o777 == 0o600


def test_concurrent_processes_never_hand_out_the_same_nonce(tmp_path) -> None:
    """The claim @yukkie3276 broke in review, now asserted instead of stated.

    The first version said "monotonic across processes" and meant "across restarts": the wall
    clock plus a process-local high-water mark keeps a restart safe and does nothing for two
    processes that load the same persisted value inside one millisecond. A concurrency claim
    with no concurrency test is how that shipped, so this forks real processes rather than
    simulating them — the bug lived precisely in what separate address spaces cannot share.
    """
    home = tmp_path / "home"
    Signer(home)  # mint the seed once, so the children only allocate
    did = Keyring(home / "seed").did
    store = home / "nonces.json"

    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client.nonces import NonceStore;"
        "s = NonceStore(sys.argv[2]);"
        "print(' '.join(str(s.allocate(sys.argv[3], 'r')) for _ in range(20)))"
    )
    # Both paths, because the package's __init__ reaches the core's `didkey` through
    # `keyring` — the child needs no key material, but importing the module still walks
    # the package.
    repo = Path(__file__).resolve().parents[2]
    client_dir = f"{repo / 'client'}:{repo / 'src'}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        outs = list(pool.map(
            lambda _: subprocess.run(
                [sys.executable, "-c", program, client_dir, str(store), did],
                capture_output=True, text=True, timeout=60, check=True).stdout.split(),
            range(6),
        ))

    nonces = [int(n) for out in outs for n in out]
    assert len(nonces) == 120
    assert len(set(nonces)) == len(nonces), "two processes were handed the same nonce"
    # And the file agrees with the highest number any of them was given: a lock that let a
    # process write a value below one already issued would leave the next run repeating it.
    assert NonceStore(store).last(did, "r") == max(nonces)


def test_the_seed_is_written_0600_and_refused_if_it_is_widened(tmp_path) -> None:
    home = tmp_path / "home"
    signer = Signer(home)
    seed_path = home / "seed"
    assert (seed_path.stat().st_mode & 0o777) == 0o600

    os.chmod(seed_path, 0o644)
    with pytest.raises(PermissionError):
        Keyring(seed_path)
    # The DID is derived, so a reload under the original mode still yields the same key.
    os.chmod(seed_path, 0o600)
    assert Keyring(seed_path).did == signer.did
