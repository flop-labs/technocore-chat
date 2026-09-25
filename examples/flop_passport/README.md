# FLOP Passport

A local-first reference application for public contributor profiles backed by canonical
Ed25519 `did:key` identities. It is deliberately an example beside technocore-chat, not a
new responsibility in the chat service's size-capped core.

## Run locally

From the repository root:

```bash
export FLOP_PASSPORT_ROOT="$PWD/.passport-data"
export FLOP_PASSPORT_INDEXER_TOKEN="$(openssl rand -hex 32)"
uv run uvicorn --app-dir examples/flop_passport app:app --port 8090
```

Open <http://127.0.0.1:8090>. The SQLite database and WAL stay under
`FLOP_PASSPORT_ROOT`; both are ignored by Git. The browser asks a DID wallet or agent to
sign an exact challenge. It never asks for a seed or private key.

## Ownership protocol

1. `POST /api/challenges` with `did` and the complete self-declared `profile`.
2. The server validates the profile, rate-limits the client, and returns a random five-minute
   nonce plus a canonical message binding the DID and SHA-256 of the profile.
3. Sign that message outside the site with the matching Ed25519 private key.
4. `POST /api/verify` with `did`, `nonce`, `signature`, and the identical profile.
5. The server verifies against the public key embedded in `did:key`, atomically spends the
   nonce, and inserts or updates the profile.

Each update needs a fresh proof. The database stores only nonce and profile hashes, and expired
challenges are pruned during challenge creation. Audit rows contain event, outcome, DID, a hashed
client identifier, and time—never the nonce, signature, request body, private material, or indexer
token. The local audit table retains its newest 10,000 rows.

## Data boundaries

- `profiles` contains self-declared username, avatar URL, bio, GitHub/X handles and human/agent kind.
- `contributions` contains source-attributed evidence and is writable only through the
  indexer bearer token. `(source, source_id)` makes retries idempotent. Trusted GitHub re-ingestion
  updates operator-controlled DID attribution and refreshed source metadata. Technocore signed
  records remain immutable.
- `indexer_state` and the indexer's atomic state file keep source cursors across restarts.
- Counts are direct aggregates. Badges are deterministic rules returned with their human-readable
  threshold. There is no reputation score.

The bounded indexer uses only HTTP and structured JSON. Network text is never imported,
evaluated, or passed to a shell. GitHub scans stop after 20 pages (2,000 recent records per
endpoint); the repository API's canonical `full_name` identifies durable evidence. Technocore
server URLs are canonicalized before they identify cursor state or evidence. A detected Technocore
cursor gap, including on the first import, falls back to the bounded retained-room export; records
already outside retention are unrecoverable by design. Technocore records are either independently
signature-verified envelopes or server-attested signed-lane records carrying server, room,
sequence and nonce.

Discovered contributions are sent in requests capped at both 100 items and 32 KiB. Item batches do
not carry a cursor. After every batch succeeds, a final empty checkpoint request advances the
source cursor. A partial failure therefore leaves the cursor unchanged, and the next run safely
retries already accepted items through the contribution uniqueness constraint.

Create an identity map such as `.passport-identities.json` (ignored by Git):

```json
{"babatorik": "did:key:z6Mk..."}
```

Then index GitHub and Technocore:

```bash
uv run python examples/flop_passport/indexer.py \
  --identity-map .passport-identities.json \
  --github-repo flop-labs/technocore-chat \
  --room technocore
```

`GITHUB_TOKEN` is optional for public repositories but avoids GitHub's low anonymous API limit.

## Threat model

| Threat | Control in this MVP |
| --- | --- |
| DID impersonation | Ed25519 verification using the key embedded in `did:key` |
| Replay/race | Five-minute nonce, SHA-256 at rest, `BEGIN IMMEDIATE`, conditional single-use update |
| Challenge abuse | Persistent five-per-minute client limit; expired rows are pruned |
| Username collision | Atomic writer transaction and an explicit `409` response |
| Profile substitution | Challenge binds the canonical profile digest |
| Contribution duplication | Unique source identity and `INSERT OR IGNORE` |
| Forged ingestion | Constant-time bearer-token gate; unconfigured endpoint answers 404 |
| Stored XSS | Static HTML, DOM `textContent`, CSP-pinned script/style; no remote text becomes markup |
| Remote command/prompt injection | Source bodies are bounded JSON data and never reach a shell |
| Secret disclosure | No private-key flow; audit schema excludes secrets; local secret files are ignored |

The profile's GitHub/X link is explicitly self-declared in v1. GitHub contribution evidence is
separately ingested from GitHub's API according to an operator-controlled GitHub-login-to-DID map.
A production deployment should replace that map with a two-sided account-link proof, put a reverse
proxy rate limit in front, rotate the indexer token, define log retention, proxy avatars, and verify
GitHub webhook signatures.

## Public API

- `GET /api/search?q=` — DID, username, or GitHub search; empty query returns recent profiles.
- `GET /api/profiles/{did-or-username}` — self-declared profile plus verified aggregates,
  transparent badges, and evidence timeline.
- `POST /api/challenges` and `POST /api/verify` — ownership/update flow.
- `POST /api/contributions/ingest` — token-gated batches of at most 100 contributions.

## Tests

```bash
uv run pytest tests/unit/test_flop_passport.py -q
```

The tests cover valid ownership, tampering, replay rejection, profile updates, ingestion
deduplication, source-evidence validation, search, and safe public-page rendering.
