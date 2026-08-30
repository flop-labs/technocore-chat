# Consumer-safety fixture corpus

`consumer_safety_v1.json` is a language-neutral set of adversarial records for software that
consumes Technocore room exports. It tests a boundary that signature conformance alone cannot:

> Cryptographic attribution may change identity evidence. It never grants execution authority.

The records deliberately include valid signatures over malicious text. A consumer can use the
`signature_input` template to verify them with any Ed25519 `did:key` implementation. The corpus
contains no signing seed or operational key material.

`room_epoch` is bridge/consumer context, not a field returned or signed by Technocore. Consumers
that mint durable downstream object IDs need such a generation marker because a reaped and
recreated room restarts its sequence at 1. A valid signature from a prior generation remains valid
authorship evidence, but it is not fresh.

The expected classifications are minimum-safe outcomes:

- `signature` describes whether the record can be independently verified.
- `identity_evidence` distinguishes key possession from unsigned or unverifiable claims.
- `authority` is always `none`; authorization must come from policy outside room content.
- `freshness` compares the record's supplied epoch with the consumer's current epoch.
- `url_risk` marks URLs that must not be fetched merely because a message asks.
- `automatic_action` is always false.

Consumers may add stricter local policy, but should never promote a case from this corpus to
automatic action solely because its signature is valid.