"""A local signer for technocore's signed lane.

Deliberately not part of the core (`src/app.py`, `src/config.py`, `src/didkey.py`,
`src/limit.py`, `src/store.py`): this is a client, it ships beside the server the way
`mcp/` does, and `sz.py`'s caps stay about the thing that has to stay small.

It does import from the core, one way only — `store.clean_text` for the sweep and
`didkey` for the DID constants — because the alternative is a second copy of two rules
that must never disagree.
"""

from .keyring import Keyring, did_from_seed
from .nonces import NonceStore
from .signer import Signer

__all__ = ["Keyring", "NonceStore", "Signer", "did_from_seed"]
