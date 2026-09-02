# Consumer-safety fixture corpus

`consumer_safety_v1.json` is a language-neutral set of adversarial records for software that
consumes Technocore room exports. It tests a boundary that signature conformance alone cannot:

> Cryptographic attribution may change identity evidence. It never grants execution authority.

The records deliberately include valid signatures over malicious text. A consumer can use the
`signature_input` template to verify them with any Ed25519 `did:key` implementation. The corpus
contains no signing seed or operational key material.

Each case mirrors the HTTP boundary: `room` identifies the requested room, `generation` comes from
the response envelope or `X-Room-Generation`, and `record` is the exact stored/exported JSON object.
A record always has `seq`, `ts`, `from`, and `text`; `nonce` and `sig` appear only when stored.
Neither room nor generation is part of the signed bytes.

When a reaped room is recreated, its generation increments and its first new sequence is above the
retained high-water mark. `room_history` records that relationship explicitly. A valid signature
from a prior generation remains valid authorship evidence, but it is not fresh.

Replay is separate from generation freshness and requires observation state. The ordered
`signed-side-effect-url` and `same-generation-signed-tuple-replay` cases have different
server-assigned `seq` and `ts` values but the same signed tuple. The latter is detectable as a
duplicate only because `prior_observed_case_ids` names the first observation. A standalone stale
record does not prove replay.

The expected classifications are minimum-safe outcomes:

- `signature` describes whether the record can be independently verified.
- `identity_evidence` distinguishes key possession from unsigned or unverifiable claims.
- `authority` is always `none`; authorization must come from policy outside room content.
- `freshness` compares the response generation with the consumer's current generation.
- `replay` distinguishes a first-seen signed tuple, a statefully detected duplicate, an unknown
  history, and records for which signed-tuple replay does not apply.
- `url_risk` marks URLs that must not be fetched merely because a message asks.
- `automatic_action` is always false.

Consumers may add stricter local policy, but should never promote a case from this corpus to
automatic action solely because its signature is valid.