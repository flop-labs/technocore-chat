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
        if ledger_existed and not seed_existed:
            try:
                keys_recorded, allocations = NonceStore(ledger).records()
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
        self.nonces = NonceStore(ledger, key_exists=seed_existed)
        # Last, so that a refusal above cannot leave a freshly minted seed behind it.
        self.keys = Keyring(seed)

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
