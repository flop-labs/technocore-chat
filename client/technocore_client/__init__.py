"""A local signer for technocore's signed lane.

Deliberately not part of the core (`src/app.py`, `src/config.py`, `src/didkey.py`,
`src/limit.py`, `src/store.py`): this is a client, it ships beside the server the way
`mcp/` does, and `sz.py`'s caps stay about the thing that has to stay small.

It does import from the core, one way only — `store.clean_text` for the sweep and
`didkey` for the DID constants — because the alternative is a second copy of two rules
that must never disagree.
"""

from .keyring import Keyring, did_from_seed
from .nonces import LedgerUnreadableError, NonceStore
from .signer import Signer

# `LedgerUnreadableError` is part of the contract, not an internal: this package refuses
# rather than guessing when the nonce record is untrustworthy, so an application has to be
# able to catch that by type. It was reachable only as `technocore_client.nonces.` until a
# test needed it, which is a caller reaching into a private module to do the documented
# thing.
__all__ = ["Keyring", "LedgerUnreadableError", "NonceStore", "Signer", "did_from_seed"]
