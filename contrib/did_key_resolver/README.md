# Offline Ed25519 did:key resolver

This small resolver expands a bare Ed25519 `did:key` into a public DID document without a
network request. It uses Technocore's `src/didkey.py` parser and PyNaCl, which is already a
project dependency. It additionally requires the key to be a valid, non-small-order Ed25519
point according to libsodium; this is stricter than Technocore's structural parser alone.

From the project root, with project dependencies installed:

```sh
PYTHONPATH=src python -m contrib.did_key_resolver \
  did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK
```

The CLI prints a JSON DID document or returns a nonzero exit status for malformed or
unsupported input. In Python:

```python
from contrib.did_key_resolver import DidResolutionError, resolve

document = resolve("did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK")
```

`resolve` raises `DidResolutionError` for malformed, unsupported, or invalid keys. The
document contains a `Multikey` verification method and references for authentication,
assertion, capability invocation, and capability delegation.

## Scope and security

- Only bare, base58btc-encoded Ed25519 `did:key` identifiers are supported. Other methods,
  key types, multibase encodings, and DID URLs with paths, queries or fragments are rejected.
- This is a minimal document, not a complete DID resolution protocol or a claim of full
  `did:key` method conformance. It does not derive an X25519 key or include `keyAgreement`.
- Resolution does not prove key possession, authenticate a message, establish reputation,
  or check whether a key is trusted. Verify the relevant signature separately.
- The JSON-LD context URLs identify vocabularies; this resolver does not fetch them.

The public example identifier comes from the
[did:key method draft](https://w3c-ccg.github.io/did-key-spec/#example-a-simple-ed25519-did-key-value).
The [DID Core specification](https://www.w3.org/TR/did-core/) defines the document model.
