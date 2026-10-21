# Contribution proof v1

This document defines the interoperable proof format used by tools that publish a
Technocore contribution. It is an offline artifact: Technocore does not store,
interpret, or endorse these proofs. A verifier needs only the proof JSON and the
Ed25519 public key encoded by its `did:key`.

## Proof object

A proof is a JSON object with these four string members:

```json
{
  "artifact_url": "https://github.com/example/project/pull/42",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "did": "did:key:z6Mk...",
  "signature": "<86-character unpadded base64url signature>"
}
```

`artifact_url` identifies the public contribution. `commit` is the lowercase commit
identifier being claimed; a verifier must not silently lowercase the value before
checking it. `did` is an Ed25519 `did:key`. `signature` is the Ed25519 signature,
encoded as unpadded base64url.

The `did` is intentionally not duplicated in the signed payload: it is the public
key used to verify the signature. The schema name *is* in the payload, preventing a
signature for a future schema from being replayed as a v1 proof.

## Canonical signed bytes

The signature covers the UTF-8 encoding of this JSON serialization:

```json
{"artifact_url":"<artifact_url>","commit":"<lowercase commit>","schema":"technocore-contribution-v1"}
```

The serialization rules are normative:

1. construct an object with exactly the keys `artifact_url`, `commit`, and `schema`;
2. set `schema` to `technocore-contribution-v1`;
3. use the lowercase `commit` exactly as supplied in the proof;
4. serialize with lexicographically sorted keys, compact separators (`,` and `:`),
   and `ensure_ascii=false` semantics;
5. encode the resulting JSON text as UTF-8;
6. sign those bytes with Ed25519;
7. encode the 64-byte signature as unpadded base64url.

For example, the payload for the JSON above is:

```text
{"artifact_url":"https://github.com/example/project/pull/42","commit":"0123456789abcdef0123456789abcdef01234567","schema":"technocore-contribution-v1"}
```

JSON whitespace, key order, escaped non-ASCII characters, a trailing newline, or a
padded signature changes the signed bytes and is not v1-compatible.

## Verification

The repository includes a dependency-isolated verifier:

```bash
uv run scripts/verify_contribution_proof.py proof.json
# VERIFIED did:key:z6Mk... 0123456789abcdef0123456789abcdef01234567
```

It fails closed for malformed DIDs, non-canonical signatures, uppercase commits,
wrong payloads, and invalid signatures. The verifier is also usable with a pipe:

```bash
cat proof.json | uv run scripts/verify_contribution_proof.py
```

This v1 rule is documented after existing publishers began using it. Existing
proofs remain verifiable; changing the canonicalization would require a new schema
name rather than silently changing v1.
