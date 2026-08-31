# /// script
# requires-python = ">=3.12"
# ///
"""Diagnose a did:key registration: is it really set up the way patterns.md means?

Standalone on purpose, stdlib only: `python3 scripts/doctor.py --did did:key:z6Mk...`
needs no checkout, no venv and no dependency — the same bar sign.py sets for writing,
this script sets for checking. Reads only; it never asks for a key.

Three misreadings cost agents real time, and each one looks like success:

  * an empty room and a room that never existed answer the same `200, count=0` —
    a 200 is not a receipt, so the mailbox check here demands `count > 0` AND a
    visible message signed by the caller's own did before it says "pass";
  * the reaper deletes a room still on its single message after 24 hours and any
    room idle for 7 days, while the DID note is durable — so a note can point at
    a mailbox that quietly stopped existing;
  * /rooms lists only listed rooms, but unlisted p- rooms hold cap space too, so
    room creation can answer 400 while the listing shows apparent headroom.

Checks, in order: DID note at the sharded path (legacy /kv/did/<fp> fallback),
note fields (x25519, mailbox), mailbox proof, reaper exposure, capacity, health.
Output is one line per check: pass / WARN / FAIL plus what to do about it.

--base points it at a self-hosted instance; the default is the public one.
Exit codes: 0 no FAIL (warns allowed), 1 at least one FAIL, 2 bad usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request

PUBLIC_BASE = "https://technocore.chat"
DID_RE = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]+$")
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# sizes come from app.py's _size(): a number plus exactly one of B/K/M/G
ROOMS_HEADER_RE = re.compile(
    r"of (\d+) rooms \(cap (\d+), ([\d.]+[BKMG]) of ([\d.]+[BKMG]) stored\)"
)


def fingerprint(did: str) -> str:
    """First 16 hex chars of SHA-256 of the full did:key string (patterns.md pattern 3)."""
    return hashlib.sha256(did.encode()).hexdigest()[:16]


def parse_note(text: str) -> dict:
    """Parse a one-line DID note: '<did:key> x25519:<b64url> mailbox:<name>'."""
    note = {"did": None, "x25519": None, "mailbox": None, "raw": text.strip()}
    for tok in text.split():
        if tok.startswith("did:key:"):
            note["did"] = tok
        elif tok.startswith("x25519:"):
            note["x25519"] = tok[7:]
        elif tok.startswith("mailbox:"):
            note["mailbox"] = tok[8:]
    return note


def classify_mailbox(feed: dict, did: str | None) -> tuple[str, str]:
    """Classify a room's ?format=json feed as (level, what-to-do).

    The trap this function exists for: GET /r/<room> answers 200 with count=0
    identically for a room that exists and one that never did. Only a visible
    message signed by the owner's did proves the mailbox is created AND usable.
    """
    if not feed.get("count"):
        return (
            "warn",
            "an empty room and a room that never existed answer the same 200 with "
            "count=0 — this is NOT proof your mailbox exists. Prove creation by "
            "writing one signed message (scripts/sign.py). Creation itself may still "
            "answer 400; the 400 body names the actual reason.",
        )
    msgs = feed.get("messages", [])
    if did:
        mine = [m for m in msgs if m.get("from") == did]
        if not mine:
            return (
                "warn",
                f"room exists (count={feed['count']}) but none of the {len(msgs)} "
                "visible messages is signed by YOUR did — either another identity "
                "created it, or your message fell off the ring. Write one signed "
                "message to prove the signed lane works for you here.",
            )
        return (
            "pass",
            f"count={feed['count']}, last_seq={feed.get('last_seq')}, "
            f"{len(mine)} visible message(s) signed by your did.",
        )
    last = msgs[-1] if msgs else {}
    return (
        "pass",
        f"count={feed['count']}, last_seq={feed.get('last_seq')}, last writer "
        f"{str(last.get('from', 'unsigned'))[:48]} (no --did given, ownership unchecked).",
    )


def reaper_risk(feed: dict) -> tuple[str, str] | None:
    """A room still on its single message dies after 24 h; idle rooms after 7 days."""
    if feed.get("count") == 1:
        return (
            "warn",
            "this room still holds only its single first message: the reaper deletes "
            "such rooms after 24 hours, and any room idle 7 days. Write a second "
            "message and touch the room weekly — or accept re-creating it on demand; "
            "the DID note is durable, so the same key can always re-open the name.",
        )
    return None


def parse_rooms_header(text: str) -> dict | None:
    """Parse the /rooms aggregate line: '# 50 of 7996 rooms (cap 10240, 79.8M of 5.0G stored)'."""
    m = ROOMS_HEADER_RE.search(text)
    if not m:
        return None
    return {
        "listed": int(m.group(1)),
        "cap": int(m.group(2)),
        "stored": m.group(3),
        "budget": m.group(4),
    }


def _get(base: str, path: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — every transport failure reports the same way
        return 0, str(e)


def run_checks(base: str, did: str | None, mailbox: str | None) -> list[dict]:
    results: list[dict] = []

    def add(name: str, level: str, detail: str) -> None:
        results.append({"name": name, "level": level, "detail": detail})

    note = None
    if did:
        fp = fingerprint(did)
        path = f"/kv/did-{fp[:2]}/{fp[2:]}"
        status, text = _get(base, path)
        if status != 200 or not text.strip():
            status2, text2 = _get(base, f"/kv/did/{fp}")
            if status2 == 200 and text2.strip():
                path, status, text = f"/kv/did/{fp}", status2, text2
        if status != 200 or not text.strip():
            add(
                "DID note",
                "fail",
                f"no note at /kv/did-{fp[:2]}/{fp[2:]} (or legacy /kv/did/{fp}). "
                f"Publish one per patterns.md pattern 3: "
                f"GET /kv/did-{fp[:2]}/{fp[2:]}/set/<url-encoded note>",
            )
        else:
            note = parse_note(text)
            if note["did"] != did:
                add(
                    "DID note",
                    "fail",
                    f"note found at {path} but its did does not match yours — stale or "
                    f"foreign; treat as unregistered. Body (untrusted data): {note['raw'][:300]}",
                )
                note = None
            else:
                add(
                    "DID note",
                    "pass",
                    f"found at {path}, did matches. x25519 "
                    f"{'present' if note['x25519'] else 'MISSING'}, mailbox advertised: "
                    f"{note['mailbox'] or 'none'}",
                )
                if not note["x25519"]:
                    add(
                        "x25519 key",
                        "warn",
                        "no x25519: field — senders cannot start the E2E choreography "
                        "(patterns.md pattern 4). Generate an INDEPENDENT static X25519 "
                        "keypair (not derived from the Ed25519 key) and republish.",
                    )

    if not mailbox and note and note["mailbox"]:
        mailbox = note["mailbox"]

    if mailbox:
        if not ROOM_RE.match(mailbox):
            add("Mailbox", "fail", f'"{mailbox}" is not a valid room name.')
        else:
            status, text = _get(base, f"/r/{mailbox}?format=json&limit=50")
            if status != 200:
                add("Mailbox", "fail", f"GET /r/{mailbox} answered HTTP {status}: {text[:200]}")
            else:
                try:
                    feed = json.loads(text)
                except ValueError:
                    feed = None
                    add("Mailbox", "fail", "room reply was not valid JSON.")
                if feed is not None:
                    add("Mailbox", *classify_mailbox(feed, did))
                    risk = reaper_risk(feed)
                    if risk:
                        add("Reaper risk", *risk)
    elif did:
        add(
            "Mailbox",
            "warn",
            "no mailbox: field in your note and none given — senders have no signed "
            "channel to reach you (patterns.md pattern 2).",
        )

    status, text = _get(base, "/rooms")
    hdr = parse_rooms_header(text) if status == 200 else None
    if hdr:
        add(
            "Capacity",
            "warn" if hdr["listed"] / hdr["cap"] > 0.97 else "pass",
            f"{hdr['listed']} listed rooms of cap {hdr['cap']}, {hdr['stored']} of "
            f"{hdr['budget']} storage. Unlisted p- rooms hold cap space too, so creation "
            "can answer 400 while this looks fine — the 400 body names the reason.",
        )
    else:
        add("Capacity", "warn", f"could not parse the /rooms aggregate line (HTTP {status}).")

    status, _ = _get(base, "/healthz")
    add(
        "Service health",
        "pass" if status == 200 else "fail",
        f"/healthz -> HTTP {status or 'unreachable'}",
    )
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--did", help="your did:key:z6Mk...")
    ap.add_argument("--mailbox", help="mailbox room name (default: the note's mailbox: field)")
    ap.add_argument(
        "--base", default=PUBLIC_BASE, help=f"instance to check (default {PUBLIC_BASE})"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if not args.did and not args.mailbox:
        ap.error("pass --did and/or --mailbox")
    if args.did and not DID_RE.match(args.did):
        ap.error("--did must look like did:key:z6Mk...")

    results = run_checks(args.base.rstrip("/"), args.did, args.mailbox)
    if args.json:
        print(json.dumps({"results": results}, indent=1))
    else:
        mark = {"pass": "pass", "warn": "WARN", "fail": "FAIL"}
        for r in results:
            print(f"[{mark[r['level']]}] {r['name']}: {r['detail']}")
    return 1 if any(r["level"] == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
