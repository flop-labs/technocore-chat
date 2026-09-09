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
from .nonces import NonceStore


class Signer:
    """A local identity: one seed, one nonce ledger, both on disk."""

    def __init__(self, home: str | Path) -> None:
        home = Path(home)
        self.keys = Keyring(home / "seed")
        self.nonces = NonceStore(home / "nonces.json")

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
