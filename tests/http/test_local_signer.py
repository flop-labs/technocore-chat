"""The local signer against the real server, not beside it.

PR #14 was closed because its client scripts were standalone: they had their own tests,
their own copy of the sweep, and nothing that ran them against the thing they had to
agree with. So every assertion here goes through the `client` fixture and the actual
app. The one that matters most is the sweep: a client that signs what the user typed is
correct on every ASCII string and refused on the first one carrying a zero-width space,
and that is the failure a standalone test cannot see.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import _client  # noqa: F401 (imported for the fixture alias below)
import pytest

import didkey
from technocore_client import Keyring, LedgerUnreadableError, NonceStore, Signer, did_from_seed
from technocore_client import nonces as nonces_module

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
        f"/kv/room-owners/d-localsigner/set-signed/{did}/{sig}/{nonce}/{quote(swept)}?if_absent=1"
    )
    assert r.status_code == 200, r.text
    # And the claim is real: the unsigned lane is now refused on that room.
    assert client.get("/r/d-localsigner/say/stranger/hello").status_code == 403


def test_note_nonces_share_one_counter_per_key_as_the_server_does(client, tmp_path) -> None:
    """`app._burn_nonce(key, nonce)` keeps one counter per note key across every namespace.

    A client keeping a counter per (namespace, key) would be tracking two where the server
    tracks one. The clock floor makes a collision unlikely rather than impossible, and unlikely
    is the word in front of every nonce bug in this package — so the scope matches the server's
    and this asserts it against the real one.
    """
    signer = Signer(tmp_path / "home")
    first = signer.nonces.allocate(signer.did, "kv:shared-key")
    did, sig, nonce, swept = signer.note("room-owners", "shared-key", signer.did)
    assert nonce > first, "a second note write on the same key must clear the first"

    # And the real server accepts a write signed under that scope.
    did, sig, nonce, swept = signer.note("room-owners", "d-notescope", signer.did)
    response = client.get(
        f"/kv/room-owners/d-notescope/set-signed/{did}/{sig}/{nonce}/{quote(swept)}?if_absent=1"
    )
    assert response.status_code == 200, response.text


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


def test_a_deleted_ledger_is_refused_in_a_fresh_process(tmp_path) -> None:
    """@yukkie3276, #803: absence was still being read as proof of a first run.

    The test this replaces deleted the ledger and allocated from a new `NonceStore` in the same
    interpreter, where `_process_floor` still held the earlier nonce — the identical test-axis
    flaw already corrected for the corrupt-ledger case, sitting in the function next to it. I
    fixed one and left its neighbour, which is the failure I had written into my own notes the
    day before.

    A fresh process has no floor, so if the clock has since moved backwards it allocates below a
    nonce already used. `Signer` now writes an empty ledger before the key exists, so a seed with
    no ledger can only mean the ledger was lost — and that is unknown, not empty.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    signer.nonces.allocate(signer.did, "room")
    (home / "nonces.json").unlink()

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "Signer(sys.argv[2])"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, "a lost ledger was accepted as a first run"
    assert "has since been lost" in result.stderr
    assert "would be refused" in result.stderr, "the error must say what it costs"


def test_a_ledger_emptied_to_braces_is_refused_beside_an_existing_key(tmp_path) -> None:
    """@Minh3132, #803: `{}` meant both "never used" and "history erased".

    The previous round closed an absent ledger, malformed JSON, and entries of the wrong shape.
    It left the one value that needs no corruption at all: truncate a populated ledger to
    exactly `{}` and it is byte-identical to what `initialise()` wrote on a first run, so every
    fail-closed check passed it. A fresh process then found no entry and no floor, allocated from
    the clock, and on a host whose clock had moved backwards issued below a nonce already spent —
    after which the server refused every signed write until wall time caught up.

    So the innocent value stopped being `{}`. Ledgers this class writes carry an initialisation
    marker, and an entry-free object without one, beside a key that exists, is an emptied file
    rather than a new one.

    No clock manipulation here, deliberately. The review asked for a rollback in the regression,
    and the refusal does not depend on one: an emptied ledger is unknown whichever way the clock
    has moved, which is a stronger guarantee than the test it was asked to make.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    issued = signer.nonces.allocate(signer.did, "room")
    ledger = home / "nonces.json"
    assert issued > 0 and json.loads(ledger.read_text()), "the ledger was not populated"
    ledger.write_text("{}")

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "s = Signer(sys.argv[2]);"
        "print(s.nonces.allocate(s.did, 'room'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"an emptied ledger was accepted and allocated {result.stdout.strip()!r}, which is not "
        "known to be above the nonce already issued"
    )
    assert "no initialisation record" in result.stderr, result.stderr
    assert "would be refused" in result.stderr, "the error must say what it costs"
    assert ledger.read_text() == "{}", "the refusal moved the evidence instead of leaving it"


def test_a_marker_of_the_wrong_shape_is_refused_like_any_other_damage(tmp_path) -> None:
    """The marker is now load-bearing, so a malformed one has to refuse rather than be ignored.

    In-process and against `NonceStore` directly, because the point is the branch and not the
    plumbing: this repository's own standard is that a refusal nobody exercises is a refusal
    nobody has checked. Treating a bad marker as absent would be the lenient reading the rest of
    this loader exists to reject — it would turn a damaged file into an entry-free one and hand
    back the clock.
    """
    path = tmp_path / "nonces.json"
    path.write_text(json.dumps({"!ledger": "v1"}))
    with pytest.raises(LedgerUnreadableError) as caught:
        NonceStore(path, key_exists=True)
    assert "not an object" in str(caught.value)
    assert path.exists(), "the refusal removed the evidence"


def test_a_stamped_ledger_with_no_entries_stays_innocent_beside_a_key(tmp_path) -> None:
    """The other half of the pair above, and the reason the marker exists rather than a ban on
    `{}`.

    Refusing every entry-free ledger beside a key would refuse the state a first run legitimately
    produces: the ledger is created *before* the key, so an install interrupted after both exist
    and before anything signs has a key and no allocations. That is innocent, and it has to keep
    starting, or the fix for an erased ledger becomes a fresh install that cannot boot.
    """
    home = tmp_path / "home"
    first = Signer(home)
    ledger = home / "nonces.json"
    assert not [k for k in json.loads(ledger.read_text()) if k != "!ledger"], (
        "this test needs the never-allocated state it is named for"
    )

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "print(Signer(sys.argv[2]).did)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"an interrupted first start was refused: {result.stderr}"
    assert result.stdout.strip() == first.did, "the second start changed identity"


def test_a_bare_store_degrades_to_the_clock_and_not_to_one(tmp_path) -> None:
    """The layer below `Signer` cannot detect a loss, so assert what it does instead.

    Second reader on this branch: replacing the old lost-ledger test removed the only coverage of
    a directly-constructed `NonceStore` — the exported surface, and the one the rest of the
    package builds on. The refusal cannot live here (a store sees a path and nothing else; only
    something that knows whether a *key* exists can tell a first run from a loss), so the honest
    thing is to state the weaker guarantee and check it: a lost ledger falls back to the wall
    clock, which is ahead of every nonce a sane clock has already issued, rather than restarting
    at 1, which is behind all of them.

    Fresh process on purpose. In-process, `_process_floor` survives the deletion and would carry
    the guarantee for free — the same test-axis flaw this branch has now fixed twice.
    """
    store_path = tmp_path / "nonces.json"
    first = NonceStore(store_path).allocate("did:key:zStub", "room")
    store_path.unlink()

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import NonceStore;"
        "print(NonceStore(sys.argv[2]).allocate('did:key:zStub', 'room'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(store_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    second = int(result.stdout.strip())
    assert second > first, "a lost ledger restarted the counter below a nonce already issued"


def test_a_ledger_with_history_and_no_key_is_refused(tmp_path) -> None:
    """@Minh3132, #803: one half of this pair failed closed and the other failed open.

    Seed present, ledger absent was already a refusal. The mirror — ledger present with
    history, seed gone — walked straight into `Keyring`, which minted a replacement. The new
    DID then reads the old ledger, finds nothing under its own name, and allocates from the
    clock as though it had never signed; meanwhile every signature and note already published
    names an identity nobody can sign as again. `Keyring` refuses to mint over an unreadable
    *existing* seed for exactly this reason, and the same fact arriving as an absent file was
    waved through.

    Fresh process, because the whole failure mode is what a restart concludes from disk.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    original = signer.did
    signer.nonces.allocate(original, "room")
    (home / "seed").unlink()

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "print(Signer(sys.argv[2]).did)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"a lost key was replaced silently; the install came back as {result.stdout.strip()} "
        f"instead of {original}"
    )
    assert "its key is gone" in result.stderr
    assert "can never sign again" in result.stderr, "the error must say what is unrecoverable"
    # And the refusal is not a one-shot — asserted by running it again rather than by checking
    # the precondition for it. `exists()` is what would have to be true for a second refusal,
    # not the refusal, and this branch has a habit of asserting the setup for a claim in place
    # of the claim.
    assert (home / "nonces.json").exists()
    again = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert again.returncode != 0, "the refusal lasted one process"
    assert "its key is gone" in again.stderr


def test_a_corrupt_ledger_beside_no_key_reports_the_lost_key(tmp_path) -> None:
    """Second reader on this branch: the wrong refusal was answering first.

    The identity check landed after the store was built, so seed-gone plus ledger-corrupt hit
    `_load`'s refusal instead — a message about lost *nonces* whose advice is to move the file
    aside once the clock has passed. Follow that here and the next start finds neither file,
    reads it as a first run, and mints the replacement identity the check exists to prevent.
    The softer message was attached to the strictly more alarming state.

    An unreadable ledger is never the innocent interrupted first start: that path writes exactly
    `{}`. Beside a missing seed it is a loss, and it is the identity that was lost.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "nonces.json").write_text("{ this is not json")

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "Signer(sys.argv[2])"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "its key is gone" in result.stderr, "the refusal named the nonces, not the identity"
    assert "can never sign again" in result.stderr
    # And it must argue against the recovery the other message recommends, which from this state
    # leads straight back to a silent first run.
    assert "Do not move the ledger aside on its own" in result.stderr
    # And the advice it argues against must not be printed alongside it. Chaining the store's
    # own refusal as the cause put both in the output: an operator reading the cause block
    # moves the ledger aside, the next start sees neither file, calls it a first run, and mints
    # the replacement. Asserting the right phrase is present says nothing about the wrong one.
    assert "moving it aside once you are satisfied" not in result.stderr, (
        "the refusal printed the recovery it exists to argue against, as a chained cause"
    )
    # The parse failure is still named, folded in rather than chained, so the operator can tell
    # a truncated file from a corrupted one.
    assert "could not be read as JSON" in result.stderr
    # Carried as data, not recovered by splitting the formatted message on its first ". " — a
    # ledger holding a repr with ". " inside it would have clipped the reason away.
    (home / "nonces.json").write_text(json.dumps({"did:key:z6MkStub": {"a. b": "x"}}))
    odd = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert odd.returncode != 0
    assert "which is not a non-negative integer" in odd.stderr, (
        "the reason was truncated at a period inside the ledger's own contents"
    )


def test_a_key_with_no_rooms_beside_no_seed_is_history_too(tmp_path) -> None:
    """Second reader on this branch: "populated" was implemented as a non-zero pair count.

    `{"did:key:z...": {}}` holds zero (key, room) pairs and is not empty. `allocate` cannot
    produce it — `setdefault(did, {})[room] = nonce` always adds a room — and `initialise`
    writes exactly `{}`, so a key sitting there with no rooms means something happened that this
    class did not do: a partial restore, a hand-edit, another tool. Summing pairs called it
    innocent and minted a replacement identity, which is the outcome the whole branch exists to
    refuse. The innocent value is `{}` and the predicate now says so.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "nonces.json").write_text(json.dumps({"did:key:z6MkStub": {}}))

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client import Signer;"
        "print(Signer(sys.argv[2]).did)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"a ledger naming a prior key was read as a first start; minted {result.stdout.strip()}"
    )
    assert "its key is gone" in result.stderr
    assert "1 prior key(s) and no allocations" in result.stderr, (
        "the message must say what was found, since it is not an allocation count"
    )


def test_an_empty_ledger_with_no_key_is_still_a_first_start(tmp_path) -> None:
    """The innocent half of the same shape, which the refusal above must not swallow.

    This class creates the ledger before the key on purpose, so ledger-present-seed-absent is
    the ordinary state of a first start that was interrupted — or of a starter that lost the
    creation race by a microsecond. History is what separates it from a lost key, which is why
    the check counts allocations rather than testing for the file.
    """
    home = tmp_path / "home"
    NonceStore(home / "nonces.json").initialise()
    assert not (home / "seed").exists()

    signer = Signer(home)
    assert signer.did.startswith("did:key:z6Mk")
    assert signer.nonces.allocate(signer.did, "room") > 0


def test_a_first_run_is_not_mistaken_for_a_loss(tmp_path) -> None:
    """The other half: a genuinely new identity must start, or the check above is a brick.

    Both files absent is a first run. The ledger is created before the key precisely so that the
    two cases stay distinguishable, and this asserts the innocent one still works — including a
    second construction, which is where an over-eager refusal would show up.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    first = signer.nonces.allocate(signer.did, "room")
    assert (home / "nonces.json").exists(), "the ledger should exist from the start"

    again = Signer(home)
    assert again.did == signer.did
    assert again.nonces.allocate(again.did, "room") > first


def test_a_corrupt_ledger_keeps_refusing_after_a_restart(tmp_path) -> None:
    """@yukkie3276, #803, twice over.

    First: the original test proved nothing, because it allocated from a new `NonceStore` in the
    same process where `_process_floor` still held the nonce issued moments earlier.

    Then: the fix that replaced it renamed the damaged file aside, which made the refusal last
    exactly one process — the next one found the canonical path absent, read it as a first run,
    and allocated from the clock with the floor still unknown. The refusal has to outlive the
    process that noticed, so this runs it twice in fresh subprocesses and then checks that
    deliberate recovery still works.
    """
    home = tmp_path / "home"
    signer = Signer(home)
    signer.nonces.allocate(signer.did, "room")
    store = home / "nonces.json"
    store.write_text("{ this is not json")

    repo = Path(__file__).resolve().parents[2]
    program = (
        "import sys;"
        "sys.path[:0] = sys.argv[1].split(':');"
        "from technocore_client.nonces import NonceStore;"
        "NonceStore(sys.argv[2])"
    )

    def fresh_process():
        return subprocess.run(
            [sys.executable, "-c", program, f"{repo / 'client'}:{repo / 'src'}", str(store)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    first = fresh_process()
    assert first.returncode != 0, "a corrupt ledger was accepted as an empty one"
    assert "could not be read as JSON" in first.stderr
    assert "would be refused" in first.stderr, "the error must say what it costs, not just fail"

    second = fresh_process()
    assert second.returncode != 0, "the refusal did not outlive the process that noticed"
    assert "could not be read as JSON" in second.stderr

    # The damaged ledger is still there, because it is the evidence and the lock at once.
    assert store.read_text() == "{ this is not json"

    # Recovery is a deliberate act by someone who has decided the clock is safely past: move it.
    store.rename(home / "nonces.json.set-aside")
    assert fresh_process().returncode == 0, "recovery left the store unusable"


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
        subprocess.Popen(
            [sys.executable, "-c", program, paths, str(home / "seed"), str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    time.sleep(0.5)  # let every child get past its imports and onto the gate
    gate.write_text("go")
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        results.append(SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr))

    failed = [r.stderr.strip().splitlines()[-1] for r in results if r.returncode != 0]
    assert not failed, f"a concurrent first start failed: {failed}"
    dids = {r.stdout.strip() for r in results}
    assert len(dids) == 1, f"first starts disagreed about the identity: {dids}"
    assert next(iter(dids)).startswith("did:key:z6Mk")
    # No temporary survived the race, and the winner's file carries the mode the loser skipped.
    assert list(home.glob("seed.*.tmp")) == []
    assert (home / "seed").stat().st_mode & 0o777 == 0o600


def test_the_ledger_is_created_before_the_key_on_a_first_run(tmp_path, monkeypatch) -> None:
    """Second reader on this branch: the loss check reopened, one level up, the race below it.

    `Keyring._load` was made safe across processes, and then `Signer.__init__` put the same
    two-process window back: it read `seed.exists() and not ledger.exists()`, then constructed
    the keyring — which may mint the seed — and only afterwards created the ledger. A second
    starter arriving between the winner's `os.link(seed)` and the winner's ledger write saw
    exactly seed-present-ledger-absent, concluded the record had been lost, and refused a
    perfectly healthy startup. My own comment claimed the opposite order was "the point" while
    the code did it backwards, which is the kind of promise the compiler does not check.

    Asserting the ordering rather than the collision, and deliberately. A gated six-process race
    passes against the broken code every time: gated starters all evaluate the check before the
    winner has minted anything, so they take the safe branch and the window is never entered. It
    needs an arrival inside a window microseconds wide — and reproducing that by sleeping is the
    flaky guard this file already refuses elsewhere. The window is closed by creating the ledger
    first, so that is what gets checked, at the syscall that publishes the key.
    """
    seen: dict[str, bool] = {}
    real_link = os.link

    def recording_link(src, dst, **kwargs):
        seen.setdefault("ledger_first", (tmp_path / "home" / "nonces.json").exists())
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", recording_link)
    signer = Signer(tmp_path / "home")

    assert seen.get("ledger_first") is not None, "the seed was never minted; test proves nothing"
    assert seen["ledger_first"], (
        "the key was published before the ledger existed, so a concurrent starter landing in "
        "that window would read a healthy first run as a lost ledger and refuse"
    )
    assert signer.nonces.allocate(signer.did, "room") > 0


def test_a_concurrent_initialiser_cannot_overwrite_a_populated_ledger(tmp_path, monkeypatch):
    """@yukkie3276, #803: the fix for a first-start race shipped a first-start clobber.

    `initialise()` checked `exists()` and then flushed, with no lock and no create-only
    publication, so two starters could both find the ledger absent. A gets there first,
    initialises, mints its key, allocates nonce N and persists it — and B, still acting on the
    answer to a question it asked before any of that happened, `os.replace`s `{}` over the top.
    Nothing was deleted and nothing was corrupted, and a durable nonce is gone anyway, which
    puts the next restart back into the unknown-floor case this change exists to close.

    The ordering test one function up cannot see this. It proves the ledger exists before the
    key is published, which is a statement about one process's sequence; this is a statement
    about two, and they are different claims.

    Deterministic, not timed. B is held inside its own `initialise` — after the stale check the
    old code made, before the flush that acts on it — while A completes in full. The gate is on
    `mkdir_durable` because it is the one call both versions make in that stretch, and it is
    held for B's thread only, so A never blocks on it. B holds no lock while it waits, so there
    is nothing here to deadlock.
    """
    home = tmp_path / "home"
    ledger = home / "nonces.json"
    b_arrived, a_done = threading.Event(), threading.Event()
    b_thread: threading.Thread | None = None
    real_mkdir = nonces_module.mkdir_durable

    def gated_mkdir(path):
        if threading.current_thread() is b_thread:
            b_arrived.set()
            assert a_done.wait(30), "the winner never finished; the race did not happen"
        return real_mkdir(path)

    monkeypatch.setattr(nonces_module, "mkdir_durable", gated_mkdir)

    b_thread = threading.Thread(target=lambda: NonceStore(ledger).initialise(), daemon=True)
    b_thread.start()
    assert b_arrived.wait(30), "the second initialiser never reached the window"

    signer = Signer(home)
    issued = signer.nonces.allocate(signer.did, "room")
    a_done.set()
    b_thread.join(30)
    assert not b_thread.is_alive()

    on_disk = json.loads(ledger.read_text())
    assert on_disk.get(signer.did) == {"room": issued}, (
        "a concurrent initialiser replaced a populated ledger with an empty one, losing a "
        "nonce that had already been issued and persisted"
    )
    # Asserted rather than dropped from the comparison: the marker is what tells the next
    # process this file was written here and not truncated to `{}`, so a flush that lost it
    # would reopen the emptied-ledger case this format exists to close.
    assert "!ledger" in on_disk, "the flush dropped the initialisation marker"
    assert set(on_disk) == {"!ledger", signer.did}, f"unexpected entries on disk: {on_disk}"


def test_a_seed_of_the_wrong_length_is_refused_at_both_doors(tmp_path) -> None:
    """The two length checks, which are the only thing standing between a truncated file and a
    signer that runs happily under an identity nobody else can verify.

    Covered explicitly because the repository's coverage floor is branch-aware for exactly this
    reason: a refusal that is never exercised is a refusal nobody has checked works.
    """
    with pytest.raises(ValueError, match="32 bytes"):
        did_from_seed(b"\x01" * 31)

    seed_path = tmp_path / "home" / "seed"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text(base64.urlsafe_b64encode(b"\x02" * 16).decode().rstrip("="))
    seed_path.chmod(0o600)
    with pytest.raises(ValueError, match="not 32"):
        Keyring(seed_path)

    # The one that matters: without validate=True, base64 silently drops characters outside its
    # alphabet, so a corrupted file decodes to *something* rather than failing. When that
    # something is 32 bytes the identity changes and nothing says so.
    seed_path.write_text("not a seed!! " + base64.urlsafe_b64encode(b"\x03" * 32).decode())
    with pytest.raises(ValueError, match="is not base64"):
        Keyring(seed_path)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('["not", "a", "map"]', "does not hold a JSON object"),
        ('{"did:key:zX": "not a room map"}', "not an object"),
        ('{"did:key:zX": {"room": "1730000000000"}}', "not a non-negative integer"),
        ('{"did:key:zX": {"room": -5}}', "not a non-negative integer"),
        ('{"did:key:zX": {"room": 1.5}}', "not a non-negative integer"),
        ('{"did:key:zX": {"room": true}}', "not a non-negative integer"),
    ],
)
def test_every_shape_the_ledger_cannot_hold_is_quarantined(tmp_path, body, reason) -> None:
    """@yukkie3276, #803: valid JSON of the wrong shape was being filtered rather than refused.

    The previous version dropped a malformed entry and carried on, and I defended that in the
    commit before this one as "readable state with a hole". It is not a hole. Dropping the entry
    *asserts* that the pair has never issued a nonce, which is the same claim the quarantine
    exists to refuse, made one level further in — and after a clock rollback it resumes below the
    server's replay floor exactly as a lost file would.

    `true` is in the table because `isinstance(True, int)` is true in Python, so a JSON `true`
    would otherwise load as the nonce 1.
    """
    store = tmp_path / "nonces.json"
    store.write_text(body)
    with pytest.raises(ValueError, match=reason):
        NonceStore(store)
    # Left exactly where it was: it is the evidence, and it is also what makes the refusal
    # outlast this process.
    assert store.read_text() == body


def test_a_well_formed_ledger_round_trips(tmp_path) -> None:
    """The other half of strictness: what the writer produces, the reader must accept."""
    store = tmp_path / "nonces.json"
    written = NonceStore(store)
    first = written.allocate("did:key:zX", "room")
    written.allocate("did:key:zX", "other")
    reread = NonceStore(store)
    assert reread.last("did:key:zX", "room") == first
    assert reread.allocate("did:key:zX", "room") > first


def test_every_successful_first_start_is_past_the_durability_barrier(tmp_path, monkeypatch) -> None:
    """@Minh3132, #803: convergence is not the same claim as durability.

    The winner links the seed's name and fsyncs the directory afterwards. A loser that read the
    winner's complete file and returned in between would hand back a working identity whose
    *name* was not yet durable — the same power-loss window the directory fsync exists to close,
    reopened for whichever process did not create the file. The existing race tests prove every
    constructor agrees on the DID; none proved every constructor had passed the barrier.

    The assertion records **which thread** performed each directory fsync, because the first
    version of this test counted them globally and therefore passed without the fix: the loser
    was being acquitted by the winner's sync. Counting is not attribution, and the property is
    about the loser.
    """
    seed_path = tmp_path / "home" / "seed"
    synced_by: set[int] = set()
    real_fsync = os.fsync
    guard = threading.Lock()

    # Attributed by (thread, inode), not merely by thread. `mkdir_durable` syncs the *parent
    # of* each directory it creates, so a thread that only made the folder would otherwise be
    # credited with syncing the folder holding the seed. Third way this test found to pass
    # without the fix; each one was found by running the control rather than by reading it.
    home_inode = {"value": None}

    def attributing_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            with guard:
                if home_inode["value"] is None and seed_path.parent.exists():
                    home_inode["value"] = seed_path.parent.stat().st_ino
                if info.st_ino == home_inode["value"]:
                    synced_by.add(threading.get_ident())
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", attributing_fsync)
    start = threading.Barrier(2)
    outcomes: list[object] = []
    idents: set[int] = set()

    def start_one() -> None:
        start.wait(timeout=10)
        try:
            did = Keyring(seed_path).did
        except Exception as exc:  # noqa: BLE001 — any raise is the failure under test
            with guard:
                outcomes.append(exc)
            return
        with guard:
            outcomes.append(did)
            idents.add(threading.get_ident())

    threads = [threading.Thread(target=start_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    assert len(set(outcomes)) == 1, f"threads disagreed about the identity: {outcomes}"
    # Both constructors returned, and *each* of them fsynced a directory itself — the loser
    # included, which is the half that was not previously true.
    assert len(idents) == 2, "both threads should have constructed a Keyring"
    assert idents <= synced_by, (
        "a constructor returned without itself syncing the directory holding the seed"
    )


def test_concurrent_first_starts_in_one_process_converge(tmp_path) -> None:
    """@yukkie3276, #803 again, and the sharp part is why the previous test could not see it.

    The staging file was named from `os.getpid()`. Separate processes get distinct names by
    construction, so the six-subprocess test above is *structurally blind* to two threads: they
    share a pid, both build the same staging path, and the loser raises at the temporary's own
    O_EXCL before ever reaching the link that handles the race. A rigorous test of the wrong
    axis is still a test of the wrong axis.

    A barrier rather than a sleep, so both threads are inside `Keyring.__init__` at the same
    moment rather than probably-overlapping.
    """
    seed_path = tmp_path / "home" / "seed"
    start = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def start_one() -> None:
        start.wait(timeout=10)
        try:
            outcome: object = Keyring(seed_path).did
        except Exception as exc:  # noqa: BLE001 — the failure mode under test is any raise
            outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=start_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    raised = [r for r in results if isinstance(r, Exception)]
    assert not raised, f"a concurrent first start raised: {raised!r}"
    assert len(set(results)) == 1, f"threads disagreed about the identity: {results}"
    # No staging file outlived the race, and the survivor carries the mode.
    assert [p.name for p in seed_path.parent.iterdir()] == ["seed"]
    assert seed_path.stat().st_mode & 0o777 == 0o600


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
        outs = list(
            pool.map(
                lambda _: subprocess.run(
                    [sys.executable, "-c", program, client_dir, str(store), did],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=True,
                ).stdout.split(),
                range(6),
            )
        )

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
