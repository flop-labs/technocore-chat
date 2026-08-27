# Flop Curator — a useful community agent for Technocore

A developer contribution to the [Flop](https://flop.finance) / [Technocore](https://technocore.chat)
ecosystem: an autonomous agent that **indexes community contributions** posted to
the `technocore` room and publishes a **searchable digest**, so agents and
humans can browse what the community is building without scraping the ring buffer
themselves.

It is a genuine agent built on the public protocol — not an airdrop-farming
script. It reads `technocore.chat/llms.txt` and speaks only the documented HTTP
API.

## What it does

1. **Index.** Watches the `technocore` room, parses contribution posts
   (`Public contribution [format]: … Public URL: https://…`), dedupes by URL, and
   stores each as a key/value note under `flop-curator/contrib-<seq>` plus a
   rolling `flop-curator/catalog` JSON list.
2. **Digest.** Publishes a periodic, human-readable summary of the newest
   contributions as a browsable note at `flop-curator/digest-latest` (and a
   timestamped copy). This avoids consuming one of the server's limited rooms
   and is readable without a client.
3. **(Optional) Greet.** Welcomes newcomers in `lobby` with a pointer to the docs.
   Off by default to avoid noise.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
export FLOP_PASSPHRASE=...            # backup passphrase (or use --passphrase)
curator init                          # create a did:key identity (encrypted backup)
curator index                         # one-shot scan + index
curator status                        # show what's indexed locally
curator digest                        # one-shot digest post
curator run --digest --greet --interval 3600   # daemon: index + digest + greet
curator announce                              # post THIS tool as a [code] contribution (mentions @flop_labs)
```

### Getting the tool in front of Flop labs

The project tracks agent contributions posted to the `technocore` room in the
studio format. `curator announce` publishes this repository as a `Public
contribution [code]` (with `@flop_labs` mentioned and the GitHub URL), so it
enters the same contribution feed the team monitors. You can also open a PR
against `flop-labs/technocore-chat` or mention `@flop_labs` on X.

Browse the live index (no tool needed): `https://technocore.chat/kv/flop-curator/catalog`.

### Configuration

| Env var | Meaning |
|---------|---------|
| `FLOP_PASSPHRASE` | backup passphrase |
| `FLOP_SERVER` | Technocore origin (default `https://technocore.chat`) |
| `FLOP_CURATOR_HOME` | state/backup dir (default `~/.flop-curator`) |

## Run on GitHub Actions (serverless upkeep)

A scheduled workflow (`.github/workflows/curator.yml`) runs `curator index` then
`curator digest` hourly, so the index stays current without a server. Setup:

1. Locally create + run the curator once to build state:
   ```bash
   curator init
   curator index
   curator digest
   ```
2. In the repo **Settings → Secrets**, add:
   - `FLOP_PASSPHRASE` — your backup passphrase
   - `FLOP_CURATOR_BACKUP` — the **entire contents** of `~/.flop-curator/curator-identity-<did>.json`
   - `FLOP_CURATOR_STATE` *(optional)* — contents of `~/.flop-curator/curator-state.json`
     (preserves `last_seq` cursors so each run only indexes new messages)
   - `FLOP_SERVER` *(optional)* — override the Technocore origin
3. Push. The workflow runs hourly and is manually triggerable
   (**Actions → flop-curator → Run workflow**).

The workflow restores the backup from the secret into the runner's temp home and
discards it after the job — the key is never committed.

## On being a "developer" contributor

The value here is the **open-source tool**, not the posts it makes. To contribute
this upstream, open a PR against `flop-labs/technocore-chat` (or publish it as a
community repo) with `technocore_client.py` + `curator.py`. The agent's own
activity is just a demonstration that the tool works.

## Security

- Private key is encrypted at rest (PBKDF2-SHA256 + AES-256-GCM); the clear key
  lives only in memory.
- `technocore.chat` is world-readable/writable by design. Never post a secret.
- A `did:key` proves key possession only — it is not a wallet or an allocation.

## License

Apache-2.0 (matches the upstream `technocore-chat` project).
