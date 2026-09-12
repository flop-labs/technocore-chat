"""The signed lane's client half: sweep, canonical string, signature, nonce.

Nothing here reimplements the sweep. `store.clean_text` is imported and called, because a
client that carries its own copy of a Unicode category rule is a copy that drifts — and
the failure when it drifts is a permanent 403 on any text containing an invisible
character, with nothing in the input to show why. Sharing the function makes parity
structural instead of something a test has to keep asserting.

The signature covers the text *after* the sweep. That is the single sharpest edge in this
protocol: the bytes the server stores are the bytes it verifies, so a client that signs
what the user typed is correct on every ASCII string and wrong on the first one carrying a
zero-width space.
"""

from __future__ import annotations

from pathlib import Path

import store

from .keyring import Keyring
from .nonces import LedgerUnreadableError, NonceStore


class Signer:
    """A local identity: one seed, one nonce ledger, both on disk."""

    def __init__(self, home: str | Path) -> None:
        """The ledger is created *before* the key, and the order is the point.

        A missing ledger is only innocent when no key exists yet. Creating it first means that
        by the time a seed is visible to anyone — including a process that lost the creation
        race — an empty ledger is visible too, so "seed present, ledger absent" can only mean
        the ledger was lost. Doing it the other way round leaves a window in which a losing
        starter sees a key with no ledger and cannot tell that from a theft of the record.
        """
        home = Path(home)
        ledger = home / "nonces.json"
        seed = home / "seed"
        # Read both before touching either: an earlier version initialised the ledger and *then*
        # asked whether it had been missing, answering a question it had just changed.
        seed_existed, ledger_existed = seed.exists(), ledger.exists()
        # Before any of the lost-state reasoning below, because that reasoning takes
        # `seed_existed` to mean "a usable key is here". A directory named `seed` makes it
        # true, and then an absent ledger was refused as "this key already exists, so a
        # ledger was written and has since been lost" — pointing the operator at the wrong
        # file for a problem that is not the ledger's. `Keyring` has the same check, and it
        # never runs, because this function answers first (own audit).
        if seed_existed and not seed.is_file():
            raise ValueError(
                f"{seed} exists and is not a regular file, so this home has no usable key "
                "and nothing here can be trusted to describe one. This is not a lost ledger: "
                "move whatever is at that path out of the way, or point this install at a "
                "different home."
            )
        # And on a first run create the ledger BEFORE the key, which is what the comment used to
        # claim while the code did the opposite. It matters under concurrency: a second starter
        # landing between the winner's `os.link(seed)` and a later ledger write would see exactly
        # seed-present-ledger-absent and refuse a perfectly healthy startup. Creating it first
        # means the window does not exist rather than being narrow.
        if not seed_existed and not ledger_existed:
            NonceStore(ledger).initialise()
        # The mirror image, which was fail-open while its twin was fail-closed (@Minh3132,
        # #803). A ledger with history beside no seed is not a first start: the key is gone, and
        # minting a replacement is the loudest possible failure wearing silence. The new DID
        # reads the old ledger, finds no entries under its own name, allocates from the clock as
        # if it had never signed — and every signature and note already published names an
        # identity nobody can ever sign as again. `Keyring` already refuses to mint over an
        # unreadable existing seed for exactly this reason; the same fact arriving as an absent
        # file was being waved through.
        #
        # Asked BEFORE the store is built, because the store's own corrupt-ledger refusal
        # otherwise answers first and answers a different question. Its advice — move the file
        # aside once you are satisfied the clock has passed — is correct for a lost record and
        # catastrophic for a lost key: follow it here and the next start finds neither file,
        # calls it a first run, and mints the replacement identity this check exists to prevent.
        # An unreadable ledger is never the innocent interrupted first start, because that path
        # writes a stamped, entry-free ledger; beside a missing seed it is a loss, and it is the
        # identity that was lost.
        #
        # An *empty* ledger beside no seed stays innocent: that is the window this class opens
        # on purpose two lines up, and the reason the test is on allocations and not on the file.
        # Live reads, not the snapshot taken before `initialise()` ran. With the snapshot, a
        # ledger carrying history that arrived after line 1 of this function — a partial
        # restore, a file-by-file sync, a `cp -r` racing a start — left `ledger_existed`
        # false, so this check was skipped and the key was minted over it. A fresh re-check
        # cannot miss a real loss: if the seed has appeared since, it is not lost (own audit,
        # same staleness @yukkie3276 found one consumer away).
        if ledger.exists() and not seed.exists():
            try:
                keys_recorded, allocations, marked = NonceStore(ledger).records()
            except LedgerUnreadableError as exc:
                # `from None`, and the reason folded into the text instead. Chaining printed the
                # store's own refusal as the cause, and that message ends "move it aside once
                # you are satisfied the clock is past the last nonce used" — the one action this
                # error exists to argue against. Suppressing the wrong answer by putting a right
                # one in front of it is not suppressing it; an operator reading the cause block
                # does the catastrophic thing anyway.
                raise self._identity_lost(
                    seed, ledger, f"exists but cannot be read ({exc.why})"
                ) from None
            # Anything but `{}` is history. Not "any allocations": a key with no rooms holds zero
            # allocations and is still not the file `initialise()` writes, and `allocate` cannot
            # produce it, so its presence means something happened here.
            if keys_recorded or allocations:
                raise self._identity_lost(
                    seed,
                    ledger,
                    f"records {allocations} nonce allocation(s)"
                    if allocations
                    else f"records {keys_recorded} prior key(s) and no allocations",
                )
            # An entry-free ledger with no marker, beside no key. Every ledger this package
            # writes is stamped, so this file did not come from here: either it is a stray that
            # something else created, or it is a ledger of ours that was emptied — and an
            # emptied one means this install had a key and has lost it as well as its history.
            # The two are indistinguishable on disk, which is exactly why this refuses instead
            # of choosing.
            #
            # I chose, one commit ago, and chose wrong. `stamp_if_empty` adopted the file so a
            # stray `{}` would stop bricking a fresh install. It also stamped the emptied case,
            # which erased the single bit that distinguished them and then let `Keyring` mint a
            # replacement identity on top — signing happily under a new DID forever, evidence
            # gone. Reproduced before reverting it. Refusing costs a fresh install one manual
            # step; stamping cost a real one its identity, silently. That asymmetry decides it.
            if not marked:
                raise self._unknown_ledger(seed, ledger)
        # One known window, left as it is on purpose. A second starter that reads
        # `seed_existed=False` after the winner's `initialise()` but before its `os.link` will
        # come through here, and if the winner's *application* has also allocated by then it
        # refuses a key that exists. Nothing allocates during `__init__` — only a later sign
        # does — so the window needs an unlucky interleaving of two different layers, and it
        # fails closed and clears on retry, which is the direction this module errs in
        # everywhere else. Recorded rather than fixed: a reader who finds it should know it was
        # seen (second reader, #803).
        # `key_exists`, not `lost`: the store needs to know a key is here even when the
        # ledger is present, because a ledger emptied to `{}` is only detectable as a loss
        # against the fact that this install has a key (@Minh3132, #803).
        #
        # And `seed.exists` rather than `seed_existed` — the method, not the snapshot. This line
        # runs BEFORE `Keyring` mints the key, so on a first run the boolean was false for the
        # life of the store: delete the ledger later in this same process, after it has signed,
        # and the lost-ledger refusal did not fire because the store still believed there was no
        # key to have lost one for (@yukkie3276, #803). Handing over the question keeps the
        # judgement here and removes the staleness.
        self.nonces = NonceStore(ledger, key_exists=seed.exists)
        # Last, so that a refusal above cannot leave a freshly minted seed behind it.
        self.keys = Keyring(seed)
        # And the ledger-versus-key comparison after that, because it needs the DID, which does
        # not exist until `Keyring` has loaded or minted the seed. That ordering does not give up
        # the property the line above buys: every state that can reach this refusal already had a
        # seed on disk when `Keyring` ran, because a ledger recording another DID *without* a seed
        # is the identity-loss gate's case and that gate answers first. So nothing was minted
        # behind this refusal in any state the gates admit.
        #
        # The residue is the window the gate cannot close — a ledger with history arriving between
        # its live re-check and `os.link(seed)`. That start does mint, and then refuses here. It
        # keeps refusing on every later start, because the seed it left derives a DID the ledger
        # still does not record, so the operator gets a repeating refusal rather than the silent
        # success that is the thing being fixed. Closing the window instead would mean deriving a
        # DID before the keyring exists, which is a second copy of the seed loader.
        recorded = self.nonces.identities()
        if recorded and self.keys.did not in recorded:
            raise self._identity_mismatch(seed, ledger, recorded, self.keys.did)

    @staticmethod
    def _identity_lost(seed: Path, ledger: Path, what: str) -> Exception:
        return ValueError(
            f"{seed} is missing, but {ledger} {what}, so this install has signed before and its "
            "key is gone. Minting a new one here would silently change identity: everything "
            "already published stays under a DID that can never sign again, and the replacement "
            "would allocate from the clock with no history. Restore the seed from a backup, or "
            "move this whole directory aside to start deliberately as a new identity. Do not "
            "move the ledger aside on its own — that turns this into what looks like a first "
            "run, which is precisely the silent replacement being refused here."
        )

    @staticmethod
    def _unknown_ledger(seed: Path, ledger: Path) -> Exception:
        """Refuse without asserting which of two things happened, because it cannot be known.

        `_identity_lost` states as fact that the install has signed before. For a ledger holding
        *entries* that is sound. For an entry-free ledger with no marker it is one of two
        readings, and the file cannot tell them apart: something else created a stray `{}`, or a
        ledger of ours was emptied — and only the second means a key existed. Reusing the
        confident wording here would have been the same defect this package keeps finding, a true
        refusal carrying a claim the evidence does not support.
        """
        return ValueError(
            f"{seed} is missing, and {ledger} exists, holds no entries and carries no "
            "initialisation record. Every ledger this package writes is stamped, so this one was "
            "not written here, and that has two readings it cannot distinguish: something else "
            "created the file and nothing has ever signed from this directory, or a stamped "
            "ledger was emptied — in which case this install had a key and both the key and its "
            "nonce history are gone. Minting a key now would be right in the first case and a "
            "silent identity replacement in the second, so it refuses and leaves the choice with "
            f"you. If nothing has ever signed from here, delete {ledger} and start again. If it "
            "has, restore the seed from a backup; a new identity will not be the old one, and "
            "everything already published stays under a DID that can never sign again."
        )

    @staticmethod
    def _identity_mismatch(
        seed: Path, ledger: Path, recorded: tuple[str, ...], did: str
    ) -> Exception:
        """Refuse a ledger whose recorded identities do not include this key's own.

        Nothing compared the two before, and the route into the gap is our own recovery advice.
        An operator who loses a seed, restores a backup and restores the wrong one gets a silent
        success: the key is present, so the identity-loss gate is skipped; the entries are
        well-formed, so `_load` accepts them; and the first `allocate` finds no record under the
        restored DID and starts from the clock — while a ledger holding the previous identity's
        entire history sits unread beside it. The install comes back up signing as somebody else
        and reports nothing wrong.

        Two readings, and the file cannot tell them apart: the wrong seed was restored, or this
        home is being reused for a new identity deliberately. Asserting either one is the defect
        this branch keeps finding — a true refusal carrying a claim its evidence does not support
        — so both are named, with the route out of each, and neither is stated as fact.

        A ledger recording no DIDs at all is not this case and stays acceptable: it is what
        `initialise()` writes, and a first run reaches here before it has allocated anything.
        """
        return ValueError(
            f"{ledger} records nonce history for {', '.join(sorted(recorded))} and none for "
            f"{did}, which is the identity the key at {seed} derives. The ledger and the key "
            "describe different identities, and that has two readings this cannot distinguish: a "
            "seed was restored from the wrong backup, in which case signing now would publish "
            "under an identity this home has never used and leave the recorded history unread — "
            "or this home is being reused for a new identity on purpose. Restore the seed that "
            "derives one of the recorded identities, or, to start deliberately as a new one, "
            f"clear this home: move or delete {seed.parent} so that nothing left here describes "
            "the old identity. Do not delete the ledger on its own — that leaves a key whose "
            "history is gone, which is the unknown-floor state refused elsewhere in this package."
        )

    @property
    def did(self) -> str:
        return self.keys.did

    def message(self, room: str, text: str) -> tuple[str, str, int, str]:
        """Sign a room message. Returns (did, signature, nonce, swept text).

        The swept text is returned rather than left implicit: it is what the caller must
        send, and handing back the original would invite signing one string and posting
        another.
        """
        swept = store.clean_text(text)
        nonce = self.nonces.allocate(self.keys.did, room)
        return self.keys.did, self.keys.sign(f"{room}|{nonce}|{swept}"), nonce, swept

    def note(self, namespace: str, key: str, value: str) -> tuple[str, str, int, str]:
        """Sign a note write. Returns (did, signature, nonce, swept value).

        The nonce is scoped to the **key alone**, not to the namespace and key together,
        because that is what the server does: `app._burn_nonce(key, nonce)` keeps one counter
        per note key across every namespace. Scoping this more finely looked harmless — the
        clock floor makes a collision unlikely — but it would have meant two counters where the
        server keeps one, and "unlikely" is the word that precedes every nonce bug in this file.
        A note nonce still shares no counter with a room's.
        """
        swept = store.clean_text(value)
        nonce = self.nonces.allocate(self.keys.did, f"kv:{key}")
        return (
            self.keys.did,
            self.keys.sign(f"{namespace}|{key}|{nonce}|{swept}"),
            nonce,
            swept,
        )
