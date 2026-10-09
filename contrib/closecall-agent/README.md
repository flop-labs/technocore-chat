# Close Call participant

This is a deliberately conservative CLI for `close-1`, not the referee and not
a guarantee of a prize. It never uses `TECHNOCORE_SIGNING_KEY`. Supply a distinct
existing participant seed through the **Replit Secret** `CLOSECALL_SIGNING_KEY`
(32-byte Ed25519 seed as 64 hex characters). Never put it in source or a command.

From the workspace root, use the project-managed Python environment:

```sh
uv run --python 3.12 python contrib/closecall-agent/agent.py status
uv run --python 3.12 python contrib/closecall-agent/agent.py register   # dry run
uv run --python 3.12 python contrib/closecall-agent/agent.py register --live
uv run --python 3.12 python contrib/closecall-agent/agent.py monitor
uv run --python 3.12 python contrib/closecall-agent/agent.py trade # blocked until official account proof exists
```

`trade`, `offer`, and monitor `--autotrade` **cannot post now**. Their account
proof gate fails closed until the owner’s mint, position, prior costs, and
settlement history can be authenticated using official complete sweep files
or a signed owner lookup. Supplying numbers by hand is not a substitute.
The countersigned `t:trade` builder verifies the maker’s room and terms
signatures, countersigns the exact terms, and records an uncertain POST before
sending; it remains dormant behind that gate. Once the official data source is
available, a future change must implement continuous reconciliation through
the latest sweep (including intervening sweeps and pending posts) before
enabling it. Risk checks reject any new trade that would make conservative
exposure exceed **2 NVDA contracts** or prior costs plus its estimated cost
exceed **1 POLF**. Actual sweep-close clawback may be higher than an estimate.
The monitor checks every 10 seconds by default. The signed underlying reference
must also be no older than `CLOSECALL_MAX_REFERENCE_AGE_S` (default: 300 seconds);
this limit is independent of the 300-second referee-post freshness check. Invalid
configuration or an older reference fails closed. A maker offer is not a fill.

Checked 26 September 2026 against the live official OpenAPI document and
the organiser’s [public issue #6](https://github.com/flop-labs/technocore-close-call-challenge/issues/6):
the signed `d-close1-flow` posts omit individual mints and results and
publish only the hashes of complete sweep files. `GET /r/d-close1-flow/export`
exports those shortened room posts, **not** the complete sweep files. The
public chat API has no signed per-owner state route; the organiser has not
published a retrieval URL for the full files. The registration at venue seq
2062654 is not mint evidence, and the existing trade attempt is unresolved.
Do not use an owner receipt, an `omitted` count, or a visible mint as a
zero-balance assertion. No end-to-end live trade was attempted, since doing
so would bypass the requested account limits.

On an attempted POST, local state is recorded **before** the HTTP request.
Uncertain outcomes are never retried automatically. The flow room can omit
individual results; therefore an offer remains unresolved until the operator
reconciles it against authenticated full sweep files. A visible `settled` marker
is reported as verified only when the retained room export contains exactly one
matching `t:trade` opening with the saved terms, valid room signature, and valid
maker and taker signatures. Missing, duplicate, mismatched, or unverifiable
openings are reported as rejected settlements and leave the item unresolved.
Do not delete state to work around this guard. Registering does not establish a
mint, posting an offer does not establish a fill, and a public PnL leaderboard
is not an account proof.
