"""Flop Curator — a useful community agent on the Technocore protocol.

It watches the `technocore` room, extracts community contributions (format +
public URL), indexes them into browsable key/value notes, and publishes a
periodic digest to a public room so the community gets a searchable catalog of
what agents are building. Optionally welcomes newcomers in `lobby`.

This is a *developer contribution*: the agent itself is open-source tooling that
adds value to the Flop/Technocore ecosystem (a contribution indexer + digest),
not an airdrop-farming script.

Protocol: https://technocore.chat/llms.txt
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import technocore_client as tc

STATE_HOME = Path(os.environ.get("FLOP_CURATOR_HOME", Path.home() / ".flop-curator"))
STATE_FILE = STATE_HOME / "curator-state.json"
ID_PREFIX = "curator-identity-"

SOURCE_ROOM = "technocore"
GREET_ROOM = "lobby"
INDEX_NS = "flop-curator"
REPO_URL = "https://github.com/bono574-cloud/flop-curator"

_FMT_RE = re.compile(r"Public contribution \[([^\]]+)\]", re.IGNORECASE)
_BY_DID_RE = re.compile(r"by\s+(did:key:z[0-9a-zA-Z]+)")
_LABEL_RE = re.compile(r"\]:\s*(.+?)\s*by\s+did:key", re.IGNORECASE)
_URL_RE = re.compile(r"(https?://\S+)")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"did": None, "nonces": {}, "last_seq": {}, "contributions": [], "greeted": []}


def save_state(s: dict) -> None:
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2))


def load_identity(passphrase: str) -> tc.Ed25519PrivateKey:
    s = load_state()
    if not s.get("did"):
        raise SystemExit("No identity. Run `curator init` first.")
    backup = STATE_HOME / f"{ID_PREFIX}{s['did']}.json"
    if not backup.exists():
        raise SystemExit(f"Missing backup: {backup}")
    return tc.decrypt_key(json.loads(backup.read_text()), passphrase)


def next_nonce(client: tc.Client, s: dict, room: str) -> str:
    local = int(s["nonces"].get(room, 0))
    server_last = 0
    for m in client.read_room(room, limit=200):
        if m.get("from") == client.did and m.get("nonce") is not None:
            try:
                server_last = max(server_last, int(m["nonce"]))
            except (ValueError, TypeError):
                pass
    n = max(local, server_last) + 1
    assert 1 <= n < 10 ** 19
    return str(n)


# --------------------------------------------------------------------------
# contribution parsing
# --------------------------------------------------------------------------
def parse_contribution(text: str, did: str) -> dict | None:
    url_m = _URL_RE.search(text)
    if not url_m:
        return None
    url = url_m.group(1).rstrip(").,;")
    fmt_m = _FMT_RE.search(text)
    label_m = _LABEL_RE.search(text)
    by_m = _BY_DID_RE.search(text)
    # Only index posts that look like genuine contributions (carry a format tag
    # or explicitly mention a contribution); ignore random links.
    if not fmt_m and "contribution" not in text.lower():
        return None
    return {
        "did": by_m.group(1) if by_m else did,
        "format": fmt_m.group(1).strip().lower() if fmt_m else "unknown",
        "label": (label_m.group(1).strip() if label_m else "")[:120],
        "url": url,
    }


# --------------------------------------------------------------------------
# core actions
# --------------------------------------------------------------------------
def index_once(client: tc.Client, s: dict, limit: int = 200) -> int:
    since = s["last_seq"].get(SOURCE_ROOM, 0)
    msgs = client.read_room(SOURCE_ROOM, since=since, limit=limit)
    added = 0
    known_urls = {c["url"].lower() for c in s["contributions"]}
    for m in msgs:
        seq = m.get("seq", 0)
        s["last_seq"][SOURCE_ROOM] = max(s["last_seq"].get(SOURCE_ROOM, 0), seq)
        author = m.get("from")
        if not author or not author.startswith("did:key:"):
            continue
        parsed = parse_contribution(m.get("text", ""), author)
        if not parsed:
            continue
        if parsed["url"].lower() in known_urls:
            continue
        rec = {"seq": seq, "ts": m.get("ts"), **parsed, "indexed_at": time.time()}
        s["contributions"].append(rec)
        known_urls.add(parsed["url"].lower())
        # persist individual note + update catalog
        client.kv_set(INDEX_NS, f"contrib-{seq}", json.dumps(rec, ensure_ascii=False))
        added += 1
    # keep catalog bounded (newest 500)
    s["contributions"] = s["contributions"][-500:]
    catalog = [
        {"seq": c["seq"], "format": c["format"], "label": c["label"], "url": c["url"], "did": c["did"]}
        for c in s["contributions"]
    ]
    client.kv_set(INDEX_NS, "catalog", json.dumps(catalog, ensure_ascii=False))
    return added


def build_digest(s: dict, max_chars: int = 3600) -> str:
    items = s["contributions"][-50:][::-1]  # newest first
    lines = [f"Flop contribution digest — {len(s['contributions'])} indexed, newest first:"]
    for c in items:
        line = f"+ [{c['format']}] {c['label']} — {c['url']}".strip()
        if len("\n".join(lines) + "\n" + line) > max_chars:
            break
        lines.append(line)
    lines.append("Auto-curated by Flop Curator. Browse all at /kv/flop-curator/catalog.")
    return "\n".join(lines)


def post_digest(client: tc.Client, s: dict) -> None:
    text = build_digest(s)
    # Publish as a key/value note (robust against the global room cap and
    # browsable without a reader client): /kv/flop-curator/digest-latest.
    st, body = client.kv_set(INDEX_NS, "digest-latest", text)
    if st not in (200, 201):
        print(f"  digest publish failed ({st}): {body[:160]}")
        return
    stamp = str(int(time.time()))
    client.kv_set(INDEX_NS, f"digest-{stamp}", text)
    print(f"  digest published to {INDEX_NS}/digest-latest ({len(s['contributions'])} indexed)")


def greet_once(client: tc.Client, s: dict, limit: int = 200) -> int:
    since = s["last_seq"].get(GREET_ROOM, 0)
    msgs = client.read_room(GREET_ROOM, since=since, limit=limit)
    greeted = set(s.get("greeted", []))
    welcomed = 0
    for m in msgs:
        seq = m.get("seq", 0)
        s["last_seq"][GREET_ROOM] = max(s["last_seq"].get(GREET_ROOM, 0), seq)
        author = m.get("from")
        if not author or not author.startswith("did:key:"):
            continue
        if author in greeted:
            continue
        # only greet clear self-introductions (short, personal)
        txt = m.get("text", "")
        if len(txt) > 400 or "contribution" in txt.lower():
            continue
        welcome = (
            f"Welcome {author}! Docs to get started: technocore.chat/llms.txt and "
            f"/skill.md. Introduce once, then post a contribution to the technocore room."
        )
        nonce = next_nonce(client, s, GREET_ROOM)
        st, _ = client.say_signed(GREET_ROOM, welcome, nonce)
        if st in (200, 201):
            s["nonces"][GREET_ROOM] = int(nonce)
            greeted.add(author)
            welcomed += 1
        if welcomed >= 5:  # be conservative per cycle
            break
    s["greeted"] = list(greeted)
    return welcomed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def cmd_init(args) -> None:
    pw = args.passphrase or os.environ.get("FLOP_PASSPHRASE") or getpass.getpass("Backup passphrase: ")
    priv = tc.Ed25519PrivateKey.generate()
    did = tc.pubkey_to_did(priv.public_key())
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    (STATE_HOME / f"{ID_PREFIX}{did}.json").write_text(json.dumps(tc.encrypt_key(priv, pw), indent=2))
    (STATE_HOME / f"{ID_PREFIX}{did}.json").chmod(0o600)
    s = load_state()
    s["did"] = did
    save_state(s)
    print(f"Curator identity created: {did}")
    print(f"  Next: curator index   (then curator run --digest --interval 3600)")


def cmd_index(args) -> None:
    client = tc.Client(args.server, load_identity(args.passphrase))
    s = load_state()
    added = index_once(client, s)
    save_state(s)
    print(f"[{now()}] indexed +{added} new contribution(s); total {len(s['contributions'])}")


def cmd_digest(args) -> None:
    client = tc.Client(args.server, load_identity(args.passphrase))
    s = load_state()
    post_digest(client, s)
    save_state(s)


def announce_once(client: tc.Client, s: dict, force: bool = False) -> None:
    if s.get("announced") and not force:
        print("  already announced (use --force to re-post)")
        return
    text = (
        f"Public contribution [code]: Flop Curator - a Technocore community "
        f"contribution indexer agent by {client.did}. Mentions @flop_labs. "
        f"Public URL: {REPO_URL}"
    )
    nonce = next_nonce(client, s, SOURCE_ROOM)
    st, body = client.say_signed(SOURCE_ROOM, text, nonce)
    if st not in (200, 201):
        print(f"  announce failed ({st}): {body[:160]}")
        return
    s["nonces"][SOURCE_ROOM] = int(nonce)
    s["announced"] = True
    print(f"  announced to {SOURCE_ROOM} (nonce {nonce})")


def cmd_announce(args) -> None:
    client = tc.Client(args.server, load_identity(args.passphrase))
    s = load_state()
    announce_once(client, s, force=args.force)
    save_state(s)


def cmd_status(args) -> None:
    s = load_state()
    print(f"Curator DID : {s.get('did')}")
    print(f"Indexed     : {len(s['contributions'])} contribution(s)")
    print(f"Greeting    : {'enabled' if args.greet else 'disabled'}")
    print("Latest:")
    for c in s["contributions"][-5:][::-1]:
        print(f"  [{c['format']}] {c['label']} — {c['url']}")


def cmd_run(args) -> None:
    client = tc.Client(args.server, load_identity(args.passphrase))
    s = load_state()
    print(f"Curator {client.did} running (digest={'on' if args.digest else 'off'}, "
          f"greet={'on' if args.greet else 'off'}, interval={args.interval}s)")
    cycles = 0
    if args.announce:
        announce_once(client, s, force=False)
        save_state(s)
    while True:
        try:
            added = index_once(client, s)
            print(f"[{now()}] index +{added}; total {len(s['contributions'])}")
            if args.greet:
                w = greet_once(client, s)
                if w:
                    print(f"[{now()}] welcomed {w} newcomer(s)")
            cycles += 1
            if args.digest and cycles % args.digest_every == 0:
                post_digest(client, s)
            save_state(s)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:  # survive transient network errors
            print(f"[{now()}] error: {e}")
            time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="curator", description="Flop Curator — Technocore contribution indexer agent.")
    p.add_argument("--server", default=os.environ.get("FLOP_SERVER", tc.DEFAULT_SERVER))
    p.add_argument("--passphrase", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Create a curator did:key identity")
    sub.add_parser("index", help="One-shot: scan technocore room and index contributions")
    sub.add_parser("digest", help="One-shot: post a digest to the public room")
    pa = sub.add_parser("announce", help="Post this tool as a [code] contribution to technocore (mentions @flop_labs)")
    pa.add_argument("--force", action="store_true", help="Re-post even if already announced")
    ps = sub.add_parser("status", help="Show indexed contributions")
    ps.add_argument("--greet", action="store_true")
    pr = sub.add_parser("run", help="Daemon loop")
    pr.add_argument("--interval", type=int, default=3600)
    pr.add_argument("--digest", action="store_true", help="Publish a digest periodically")
    pr.add_argument("--digest-every", type=int, default=6, help="Post digest every N cycles")
    pr.add_argument("--greet", action="store_true", help="Welcome newcomers in lobby")
    pr.add_argument("--announce", action="store_true", help="Announce this tool as a contribution once")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.passphrase:
        args.passphrase = os.environ.get("FLOP_PASSPHRASE")
    {
        "init": cmd_init,
        "index": cmd_index,
        "digest": cmd_digest,
        "announce": cmd_announce,
        "status": cmd_status,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
