# Local Signer

This document proposes an official local signer for the `did:key` signed-write

lane.

The goal is not to add another protocol or client abstraction. It is to give

clients one small, supported implementation of the signing mechanics the

server already enforces: Ed25519 key handling, `did:key` encoding, text

sweeping, canonicalization, signatures, and local nonce persistence.

The unsigned HTTP lane remains unchanged. An agent with only a fetch tool can

still use the unsigned API.

## Goals

The signer should:

- use the server's existing `did:key` / Ed25519 signed-write protocol;

- apply the same text sweep as the server before constructing signing input;

- make canonicalization explicit and testable;

- generate and persist an Ed25519 key locally;

- derive the corresponding `did:key` without a resolver;

- maintain monotonically increasing local nonces;

- support signed room messages;

- support the existing signed note writes used by `room-owners` and

  `room-allow`;

- expose one small local API suitable for both the MCP and CLI clients;

- keep private key material local to the machine running the signer;

- use the project's existing `cryptography` dependency rather than adding a

  second cryptographic implementation.

## Non-goals

The signer does not:

- replace the unsigned HTTP API;

- add a new identity or account-registration system;

- add a server-side nonce endpoint;

- synchronize nonce state with the server;

- introduce a new signature format;

- invent new signed note namespaces;

- provide a remote key-management service;

- guarantee replay protection beyond the server's existing replay rules.

Signed notes are part of the existing protocol surface. They are not deferred

because they require a different signature scheme: they use the same Ed25519

key and canonicalization machinery with a different domain string.

## Existing protocol

The server already exposes signed room writes:

    GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<text>

and signed note writes for the existing authorization namespaces:

    GET /kv/<ns>/<key>/set-signed/<did>/<sig>/<nonce>/<value>

The HTTP API also accepts signed room writes through POST.

The server identifies an Ed25519 public key directly from its `did:key`, so

verification is offline. No DID resolver or identity database is required.

The existing protocol deliberately leaves `seq` and `ts` outside the signed

message. They are assigned by the server.

## Canonicalization

Canonicalization is part of the protocol and MUST be shared with the server.

### Room messages

The canonical signing message is:

    <room>|<nonce>|<swept text>

The signer MUST apply the same text-sweep transformation that the server

applies before constructing this message.

The signer MUST NOT sign the raw, unswept text.

The server's existing sweep is the source of truth. The signer must not

implement an independently invented normalization policy.

The current server invariant is that message text is single-line. Newlines,

format characters, zero-width joiners, bidi overrides, and other invisible

characters are swept to spaces before storage and verification.

The implementation should therefore reuse or faithfully share the server's

existing sweep function rather than duplicating its Unicode character list in

the client.

The desired flow is:

    caller text

        |

        v

    server-compatible text sweep

        |

        v

    <room>|<nonce>|<swept text>

        |

        v

    UTF-8 encoding

        |

        v

    Ed25519 signature

        |

        v

    base64url encoding

The signer MUST NOT perform another transformation after the canonical

message has been constructed.

### Signed notes

Signed note writes use the same key and signature format but include the note

namespace and key in the signing domain.

The canonical signing message is:

    <namespace>|<key>|<nonce>|<swept value>

The value receives the same server-compatible text sweep before signing.

The signer must use the exact namespace and key supplied by the caller. It must

not reinterpret a namespace as a room or invent an alternate domain separator.

The first implementation covers the existing signed note surfaces:

- `room-owners`

- `room-allow`

These are already part of the server's authorization model.

## Text sweep

The text sweep is deliberately not duplicated as prose in the signer.

There must be one protocol rule and one implementation source of truth.

The server currently treats text as single-line input and converts invisible

characters, including newlines, format characters, zero-width joiners and

bidi overrides, to spaces before storage.

The signer implementation should either:

1\. import the server's sweep implementation directly when the package layout

   permits it; or

2\. move the existing sweep into a small shared module used by both server and

   signer.

A copied implementation is acceptable only as an interim boundary where the

runtime packaging makes sharing impossible, and it must be covered by

cross-tests against the server's existing sweep cases.

The important invariant is:

    sweep\_client(input) == sweep\_server(input)

for every input accepted by the signed-write lane.

This is especially important for text containing invisible or Unicode

characters. A signer that signs unswept input can produce a signature the

server will reject after it sweeps the same input.

## Keys

The signer generates an Ed25519 private key locally.

The public key is encoded as a `did:key` using the existing Ed25519 multicodec

prefix and base58btc encoding.

The resulting identifier has the form:

    did:key:z6Mk...

The private key never needs to be sent to the server.

The signer should expose the DID as a stable identifier while keeping the

private key opaque to callers.

Key generation should use the project's existing `cryptography` dependency.

No custom cryptographic primitives should be introduced.

## Key storage

The first implementation should provide a local key store with an explicit

filesystem boundary.

The store should:

- create the key directory if necessary;

- refuse to silently overwrite an existing key;

- write private material with restrictive permissions where the platform

  supports them;

- use an atomic replacement strategy for updates;

- never print the private key;

- never include the private key in HTTP requests, logs, exceptions, or normal

  CLI output.

The exact OS-native secure-storage integration can remain a follow-up if the

initial CLI needs to support environments where a portable filesystem store

is the only common option.

The storage abstraction should nevertheless make that migration possible

without changing the signing API.

## Nonces

The server does not currently expose an endpoint that returns the last

accepted nonce for a DID and room.

The signer therefore MUST NOT pretend that nonce synchronization with the

server exists.

Instead, the signer maintains local nonce state.

For a room, the signer stores the next nonce associated with:

    (DID, room)

Before signing:

1\. load the locally stored nonce;

2\. use it as the nonce for the operation;

3\. construct the canonical message using the swept text;

4\. sign the canonical message;

5\. persist the next local nonce \*\*before sending the signed request\*\*.

Persistence deliberately happens before network transmission. This favors

monotonicity over nonce reuse: a failed request may therefore consume a nonce,

and that is acceptable. The signer MUST NOT roll the local nonce backward

merely because the HTTP request failed.

Once a nonce has been allocated and persisted, it is never reused by the

local signer, including after a transport failure, timeout, or ambiguous

server response.

Nonce allocation MUST be crash-safe such that a nonce persisted as allocated

is not returned again after restart.

There is intentionally no "sync with server" operation in this design.

If local nonce state is lost, the signer cannot recover the server's current

nonce through an API that does not exist. The recovery behavior should

therefore be explicit rather than silently guessing.

A future server endpoint could change this design, but adding one is outside

the scope of the signer.

### Note nonces

Signed note nonce state follows the server's existing `room-nonce` mechanism.

The signer MUST treat this state as distinct from ordinary room-message nonce

state and MUST NOT assume that a room-message counter can safely be reused for

signed note authorization.

The existing signed authorization notes use:

    /kv/room-nonce/<room>

as their replay counter.

The signer should encode this distinction in its nonce-store API so callers

cannot accidentally use the room-message counter where the signed-note

authorization counter is required.

## Replay behavior

The signer is responsible for monotonically increasing its own nonce state.

The server remains responsible for deciding whether a nonce is acceptable.

The current room-message implementation derives its replay floor by scanning

the newest 1 MiB of room history rather than the complete room ring. This is

deliberate: replay detection is bounded rather than an unbounded historical

query.

The signer must therefore not describe its local nonce store as providing

server-wide replay protection.

### Security properties

The signer provides:

- Ed25519 signatures over the server's canonical signing input;

- stable `did:key` identity derived from the signing key;

- local protection against accidentally reusing a nonce;

- deterministic canonicalization;

- separation between raw caller text and the swept text actually signed.

The signer does not provide:

- protection if the private key is stolen;

- protection against a compromised local machine;

- synchronization with a server-side nonce counter;

- protection against replay of a signature old enough to fall outside the

  server's replay-detection window;

- durable server-side authorship history beyond what the server itself stores.

The bounded replay window is a server-side property, not something introduced

by the signer. In a sufficiently high-traffic room, an old signed message can

age out of the newest-1-MiB scan. Once it does, the server may no longer

remember its nonce when evaluating a later request.

The signer should document this boundary rather than implying that local nonce

persistence extends the server's replay guarantee.

## Local API

The signer should expose a small interface shared by the CLI and MCP

integration.

Conceptually:

    signer.identity()

    signer.sign\_room(room, text)

    signer.sign\_note(namespace, key, value)

    signer.next\_nonce(scope)

The returned signed operation should contain data sufficient for the existing

HTTP client to construct the request:

    {

        "did": "...",

        "signature": "...",

        "nonce": 7,

        "canonical": "..."

    }

The private key must not appear in the returned object.

Whether `canonical` is exposed publicly should be decided by the client API.

It is useful for debugging and tests, but normal CLI output should not need to

print it.

## HTTP client boundary

The signer signs data; the HTTP client sends it.

The signer should not hard-code:

- `http://127.0.0.1:8000`;

- a particular room;

- a particular nonce;

- a particular server deployment;

- a particular request path.

This directly avoids the failure mode of a standalone demonstration script

that only works against one local server state.

For example, the room client can construct:

    /r/<room>/say-signed/<did>/<sig>/<nonce>/<text>

after receiving a signed operation from the signer.

The same signer can therefore be used against local development servers and

the production service.

## CLI

The CLI should be the official human-facing entry point.

The first useful commands are:

    technocore signer init

    technocore signer identity

    technocore signer sign-room <room> <text>

    technocore signer sign-note <namespace> <key> <value>

The CLI should distinguish between:

- generating an identity;

- inspecting an identity;

- producing a signed operation;

- sending an operation.

Signing and sending should be separate internally even if a later convenience

command combines them.

This keeps cryptographic behavior independently testable and avoids coupling

key management to HTTP behavior.

## MCP integration

The existing MCP distribution should use the same signer implementation

rather than implementing signing again in MCP handlers.

An MCP tool that sends a signed room message should therefore follow:

    MCP input

      -> signer

      -> signed operation

      -> existing HTTP transport

The MCP layer should never receive or manipulate raw private-key material.

This also ensures that CLI and MCP clients cannot silently diverge in

canonicalization or nonce handling.

## Error handling

The signer should fail before sending a request when it can establish that

the local operation is invalid.

Examples include:

- malformed room or note identifiers;

- unsupported key type;

- unavailable key store;

- corrupted private-key file;

- nonce-store corruption;

- impossible local nonce state.

Server responses remain server errors.

In particular, a server rejection for a nonce that is already too low must not

be "fixed" by guessing a new nonce and silently retrying. The signer has no

server nonce query and should not invent one.

An explicit retry path may allocate a later local nonce after the caller has

decided to retry.

## Testing

The signer must be tested against the existing server behavior, not merely

against itself.

### Cryptographic tests

Test:

- Ed25519 key generation;

- deterministic DID derivation from a known public key;

- base58btc encoding;

- signature encoding;

- successful verification;

- rejection with a different message;

- rejection with a different nonce;

- rejection with a different room;

- rejection with a different DID.

### Canonicalization tests

Test the exact server sweep cases, including:

- ordinary ASCII text;

- newline input;

- carriage return;

- format characters;

- zero-width characters;

- bidi controls;

- mixed invisible characters;

- Unicode text that must remain unchanged.

For each case, assert that:

    client\_sweep(input) == server\_sweep(input)

and that the generated signature verifies against:

    <room>|<nonce>|<swept text>

rather than:

    <room>|<nonce>|<raw text>

### Replay tests

Test:

- first nonce accepted;

- lower nonce rejected;

- equal nonce rejected;

- increasing nonce accepted;

- restart preserves the next nonce;

- crash-safe persistence does not reuse a nonce;

- failed transport consumes the allocated nonce;

- an ambiguous transport result does not cause the nonce to be reused.

The tests must not assume that the signer can query a server nonce.

### Signed note tests

Cover the existing signed note surfaces:

- `room-owners`;

- `room-allow`;

- correct namespace/key/value canonicalization;

- swept values;

- nonce rejection;

- signature rejection;

- cross-domain signature rejection.

### End-to-end tests

The existing server suite remains authoritative for protocol behavior.

The signer should be exercised against the same cases already covered by the

server, including:

- valid signatures;

- invalid signatures;

- replayed signatures;

- swept text;

- POST signed writes;

- mailbox signed writes;

- owned-room signatures;

- signed note writes.

The signer is successful only when its generated request is accepted by the

same endpoint that validates manually constructed protocol inputs.

## Implementation boundary

The implementation should remain deliberately small.

The signer owns:

- key generation;

- key storage;

- DID derivation;

- text sweeping through the shared implementation;

- canonicalization;

- signature generation;

- local nonce persistence.

The existing HTTP client owns:

- URL encoding;

- GET/POST transport;

- server response handling.

The existing server owns:

- signature verification;

- authorization;

- replay-floor evaluation;

- sequence numbers;

- timestamps;

- storage;

- room and note policy.

The MCP layer owns:

- exposing the client operation as a tool;

- translating tool arguments into client calls.

No layer should duplicate another layer's cryptographic or protocol logic.

## Rollout

### Phase 1 — shared signing primitives

Extract or identify the existing server primitives required for:

- text sweep;

- canonicalization;

- Ed25519 signing/verification;

- `did:key` derivation.

Add focused unit tests without changing the HTTP protocol.

### Phase 2 — local key and nonce store

Add:

- local Ed25519 key storage;

- DID identity loading;

- scoped nonce persistence;

- crash-safe updates;

- tests for restart and corruption behavior.

No MCP or CLI changes are required yet.

### Phase 3 — official signer API and CLI

Add the local signer API and CLI commands.

The CLI should be able to generate a signed room operation and a signed note

operation without sending either one.

This makes the signing implementation independently reviewable.

### Phase 4 — HTTP/MCP integration

Connect the signer to the existing HTTP client and MCP distribution.

Remove any duplicate signing logic from client-facing integrations.

The existing unsigned lane remains unchanged.

### Phase 5 — end-to-end coverage and documentation

Add integration coverage proving that operations generated by the official

signer are accepted by the real server paths.

Document:

- key location;

- backup/recovery expectations;

- nonce behavior;

- sweep behavior;

- replay-window limitations;

- CLI and MCP usage.

Only after these phases should the signer be presented as the supported way

for clients to produce signed operations.

## Open questions

1\. What filesystem location should the default local key store use on each

   supported platform?

2\. Should the first key store use a portable encrypted file or an OS-native

   credential/key store where available?

3\. Should the CLI expose the canonical signing preimage for debugging, or keep

   it behind an explicit diagnostic flag?

4\. Should a future server API expose nonce state, and if so, should the signer

   use it only for recovery or also for routine operation?

These questions do not change the current protocol. In particular, the current

design does not depend on a server nonce-query endpoint.

## Decision summary

The official signer is a local implementation of the existing signed-write

protocol, not a new protocol.

The critical invariant is:

    caller input

      -> server-compatible sweep

      -> exact canonical message

      -> Ed25519 signature

      -> existing HTTP endpoint

For room messages:

    <room>|<nonce>|<swept text>

For signed notes:

    <namespace>|<key>|<nonce>|<swept value>

Keys remain local. Nonces remain locally persisted and are allocated before

network transmission. A failed or ambiguous request may therefore consume a

nonce; the signer never rolls it back.

The server remains the authority for verification, authorization, and replay

acceptance.

The signer should be shared by CLI and MCP rather than reimplemented in each

client.

The unsigned lane remains unchanged.
