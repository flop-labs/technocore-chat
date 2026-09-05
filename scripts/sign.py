# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""A minimal Ed25519 did:key signer for technocore-chat's signed lane.

Standalone on purpose: 'uv run scripts/sign.py ...' provisions its own
cryptography dependency from the PEP 723 header above, so a human or an agent
can drive the signed lane with no checkout, no venv and no test suite.

The whole point of this file is the canonical string. The server verifies a
signature over exactly what it stores:

    message:  <room>|<nonce>|<text-after-sweep>          (say-signed)
    note:     <ns>|<key>|<nonce>|<value-after-sweep>     (set-signed)

And one the server does not verify at all, because nothing on it has to:

    delegate: delegate|<root-did>|<agent-did>|<scope>|<expires>|<nonce>

That is a *delegation*: a root key saying "this agent key acts for me, this
much, until then". It is published as a line in the root's own DID note and
verified by whoever cares, offline, against the root's did:key — so the record
carries its own proof, and the note holding it needs no protection. Anyone may
overwrite that note; a forged line simply fails to verify. See 'check'.

Note the leading literal. Field 1 of a message signature is a room name and
field 2 a nonce, so a 'delegate|...' string can be read as neither of the two
above, and a signature over one is never a valid signature over the other.

"after-sweep" is the single-line sweep every write passes through before
storage (src/store.py clean_text): each character whose Unicode category is
Cc, Cf, Cs, Co, Zl or Zp becomes a space, then the ends are trimmed. Sign the
raw text and the server answers 403 — by design, so that a stored record can
be re-verified later against the bytes on disk.

Key material comes from --seed or $SIGN_SEED:
  * 64 hex characters   -> used directly as the 32-byte Ed25519 seed
  * anything else       -> SHA-256 of it (so a passphrase works; weaker than
                           randomness, fine for a demo, not for a identity you
                           care about)
  * neither given       for 'keygen': 32 random bytes, printed so you can reuse

Usage:
  uv run scripts/sign.py keygen
  uv run scripts/sign.py did      [--seed HEX|PASSPHRASE]
  uv run scripts/sign.py say      [--seed ...] <room> <nonce> <text>
  uv run scripts/sign.py set      [--seed ...] <ns> <key> <nonce> <value>
  uv run scripts/sign.py note     <did:key>
  uv run scripts/sign.py delegate [--seed ...] <agent-did> <scope> <days> [nonce]
  uv run scripts/sign.py check    <root-did> [file]

'keygen' prints the seed and the did:key. 'did' prints the did:key. 'say' and
'set' print two lines — the did:key, then the 86-character base64url
signature — ready for:

  GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<url-encoded text>
  GET /kv/<ns>/<key>/set-signed/<did>/<sig>/<nonce>/<url-encoded value>

Nonces are yours to choose (1-19 digits) and must count up per key per room;
a millisecond clock works, and so does a plain counter.

'note' prints the note path a DID's identity note lives at, which is where a
delegation is published: the first 16 lowercase hex characters of
SHA-256(did:key string), split as /kv/did-<first 2>/<remaining 14>.

'delegate' prints one `delegate: ...` line to add to that note. Scope is '*',
'r:<room>' or 'kv:<ns>'; <days> is how long it stays valid, and the nonce
defaults to a millisecond clock. Expiry is the ONLY revocation this format has
— a reader holding a cached copy of the note cannot see a line you deleted —
so delegate for days, not years, and re-issue.

'check' reads a note (a file, or stdin) and reports every delegation in it
against a root did:key: which verify, which have expired, which are forged,
and which a later re-issue has superseded. Where two records name one agent the
higher nonce wins — re-issuing is how a delegation stays alive, and without that
rule putting an old record back would undo a narrowed scope.
Records are found by scanning for the `delegate:` token, not by line — a note is
always one line, because the server's sweep turns every newline into a space.
It needs no key and no network, which is the property that matters: a
delegation is checkable by anyone, from the note text alone.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
import time
import unicodedata
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "did:key:z6Mk"  # multibase 'z' + the fixed ed25519-pub prefix base58-encodes to z6Mk
MULTICODEC_ED25519 = b"\xed\x01"  # varint ed25519-pub, the two bytes every z6Mk key decodes from
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# The sweep, mirrored from src/store.py clean_text: these are the categories it
# replaces with a space. Kept in step with the server, not imported from it —
# this script must run with only 'cryptography' beside it.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")

MAX_TEXT_CHARS = 4096  # messages
MAX_VALUE_CHARS = 8192  # notes

# A delegation's scope. Deliberately three shapes and no grammar to speak of: this file
# issues and checks delegations, it does not enforce them, and a scope language richer than
# what a reader can act on is a language nobody implements the same way twice.
#   *        everything the root could do
#   r:<room> one room
#   kv:<ns>  one note namespace
# The room/namespace halves are store.py's NAME_RE, so a scope names something that can
# exist.
NAME = r"[a-z0-9][a-z0-9_-]{0,47}"
SCOPE_RE = re.compile(rf"\*|r:{NAME}|kv:{NAME}")
DIGITS_RE = re.compile(r"[0-9]{1,19}")
# `mailbox: <room>` already lives in this note (see the manual's IDENTITY section), so a
# delegation goes in the same place rather than in a second note to find.
#
# A *token*, not a line prefix, because a note has no lines. store.clean_text replaces every
# Cc character with a space -- and U+000A is Cc -- so a note is strictly one line however it
# was written, and a second record separated by a newline arrives glued to the first by a
# space. Records are therefore found by scanning for this token and taking the five fields
# after it, which reads `mailbox: mb-x delegate: <did> ...` correctly and needs no delimiter
# the sweep could eat.
DELEGATE_TOKEN = "delegate:"
DELEGATE_FIELDS = 5  # agent, scope, expires, nonce, sig


def swept(text: str, limit: int) -> str:
    """The text as the server will store it: invisibles -> spaces, trimmed.

    Raises on what the server would refuse anyway (nothing visible left, or
    over the cap), so a caller learns it here rather than from a 4xx.
    """
    cleaned = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()
    if not cleaned:
        raise SystemExit(
            "nothing visible would be left after the single-line sweep — the server "
            "refuses that write, so there is nothing worth signing"
        )
    if len(cleaned) > limit:
        raise SystemExit(
            f"{len(cleaned)} characters after the sweep, over the {limit}-character cap — split it"
        )
    return cleaned


def multibase(raw: bytes) -> str:
    """base58btc, the multibase a did:key segment is written in."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return out


def load_key(seed_arg: str | None) -> tuple[Ed25519PrivateKey, str]:
    """The Ed25519 key for --seed / $SIGN_SEED, plus a human-readable provenance."""
    given = seed_arg or os.environ.get("SIGN_SEED")
    if given is None:
        raise SystemExit("no key: pass --seed <hex|passphrase> or set $SIGN_SEED")
    if len(given) == 64:
        try:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(given)), given
        except ValueError:
            pass  # 64 chars but not hex — fall through and hash it like any passphrase
    digest = hashlib.sha256(given.encode()).hexdigest()
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(digest)), f"sha256({given!r})"


def did_of(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes_raw()
    mb = "z" + multibase(MULTICODEC_ED25519 + raw)  # multibase tag + base58btc; fixed 'z6Mk' head
    if len(mb) != 48:  # 2 codec bytes + 32 key bytes base58-encode to 48 chars, always
        raise SystemExit(f"internal: bad multibase length {len(mb)}")
    return "did:key:" + mb


def signature(key: Ed25519PrivateKey, message: str) -> str:
    """86 unpadded base64url characters, the encoding the server's SIG_RE expects."""
    raw = key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unbase58(raw: str) -> bytes:
    """The inverse of multibase(), for reading a did:key back into a public key.

    Only 'check' needs this — issuing a delegation never parses one — which is why it sits
    apart from the encode path rather than beside it.
    """
    n = 0
    for ch in raw:
        digit = B58.find(ch)
        if digit < 0:
            raise SystemExit(f"bad did:key: {ch!r} is not base58btc")
        n = n * 58 + digit
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""


def public_key(did: str) -> Ed25519PublicKey:
    """The Ed25519 public key inside a did:key, or exit. Mirrors src/didkey.py public_key:
    same length check, same multicodec check, same refusal of everything that is not
    ed25519-pub."""
    if not did.startswith("did:key:z"):
        raise SystemExit(f"bad did:key: expected did:key:z6Mk..., got {did!r}")
    mb = did[len("did:key:") :]
    if len(mb) != 48:
        raise SystemExit(f"bad did:key: expected 48 multibase characters, got {len(mb)}")
    decoded = unbase58(mb[1:])
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        raise SystemExit("bad did:key: only ed25519-pub (z6Mk...) keys are accepted")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


def note_path(did: str) -> str:
    """Where a DID's identity note lives — the manual's IDENTITY section, in code.

    Fingerprint over the did:key *string*, not the key bytes: the string is what every
    reader already has, and hashing the bytes would make the path unreachable from a DID
    printed in a message.
    """
    public_key(did)  # refuse to name a path for something that is not a key we accept
    fingerprint = hashlib.sha256(did.encode()).hexdigest()[:16]
    return f"/kv/did-{fingerprint[:2]}/{fingerprint[2:]}"


def delegation(root: str, agent: str, scope: str, expires: str, nonce: str) -> str:
    """The canonical string a delegation signature covers.

    The root DID is *in* the signed string even though the note it lands in is already
    addressed by the root's fingerprint. Without it, a line lifted out of one root's note
    and pasted into another's would carry a signature that still verified against whichever
    key the reader happened to be checking — the path is not part of the proof, so the proof
    has to name its own issuer.
    """
    return f"delegate|{root}|{agent}|{scope}|{expires}|{nonce}"


def delegations(body: str) -> list[tuple[str, str, str, str, str]]:
    """Every delegation record in a note, found by token rather than by line.

    Scans the whole note for DELEGATE_TOKEN and takes the five fields after each. That is
    what makes several records survive in one note: they are separated by whitespace, which
    is all a note can hold, and the read lane's banner and budget footer contain no such
    token so they fall out for free.
    """
    fields = body.split()
    out = []
    for i, token in enumerate(fields):
        if token != DELEGATE_TOKEN:
            continue
        record = fields[i + 1 : i + 1 + DELEGATE_FIELDS]
        if len(record) == DELEGATE_FIELDS:
            out.append((record[0], record[1], record[2], record[3], record[4]))
    return out


def newest(records: list[tuple[str, str, str, str, str]]) -> set[int]:
    """The indices of the record that wins for each agent: highest nonce, ties to the last.

    Re-issuing is the documented way to keep a delegation alive, since expiry is the only
    revocation this format has — so a note accumulates several records naming one agent, and
    something has to say which of them is current. The nonce does, exactly as it does for a
    signed message.

    This is not tidiness. The note is world-writable, so anyone can re-add a *superseded*
    record: it was really signed by the root and it may not have expired, so it verifies. If
    every valid record counted, narrowing a delegation from `*` to `r:lobby` could be undone
    by putting the old one back. Highest-nonce-wins makes that a no-op.
    """
    best: dict[str, tuple[int, int]] = {}
    for i, (agent, _scope, _expires, nonce, _sig) in enumerate(records):
        rank = int(nonce) if nonce.isdigit() else -1
        if agent not in best or rank >= best[agent][0]:
            best[agent] = (rank, i)
    return {i for _rank, i in best.values()}


def check_note(root: str, body: str) -> int:
    """Report every delegation in `body` against `root`. Returns the count that verify.

    Prints one line per delegation and never raises on a bad one: the whole point of a
    self-certifying record in a world-writable note is that garbage is *expected* and is
    supposed to be visibly inert rather than fatal.
    """
    key, live, now = public_key(root), 0, int(time.time())
    records = delegations(body)
    current = newest(records)
    for i, (agent, scope, expires, nonce, sig) in enumerate(records):
        try:
            key.verify(
                base64.urlsafe_b64decode(sig + "=="),
                delegation(root, agent, scope, expires, nonce).encode(),
            )
        except (InvalidSignature, ValueError, TypeError):
            # Not "invalid": *forged, or for somebody else*. A record that fails here was
            # signed by a key that is not this root, which in a note anyone can write to is
            # the ordinary case and not an error.
            print(f"FORGED     {agent} {scope}  (not signed by {root[:20]}...)")
            continue
        if not expires.isdigit() or int(expires) <= now:
            print(f"EXPIRED    {agent} {scope}  (expired {expires})")
            continue
        # Checked after the signature and the expiry, so a superseded record is only ever
        # reported as superseded when it was otherwise a real, live grant.
        if i not in current:
            print(f"SUPERSEDED {agent} {scope}  (a higher nonce than {nonce} names this key)")
            continue
        # Rounded up: a delegation issued for 30 days is "30d left" a second later, where
        # flooring would report 29 and make every fresh delegation look already shortened.
        left = -((now - int(expires)) // 86400)
        print(f"OK         {agent} {scope}  ({left}d left, nonce {nonce})")
        live += 1
    return live


def main() -> None:
    # --seed lives on a parent parser so it reads naturally on either side of the
    # subcommand: 'sign.py --seed X say ...' and 'sign.py say --seed X ...' both work.
    # default=SUPPRESS is what makes that true: without it, each subparser's inherited
    # copy of the option re-defaults the attribute to None AFTER the top-level parse
    # already stored X, silently discarding it (review: PR #54).
    seeded = argparse.ArgumentParser(add_help=False)
    seeded.add_argument(
        "--seed",
        default=argparse.SUPPRESS,
        help="64-hex-char seed, or any string (hashed with SHA-256)",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], parents=[seeded])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", parents=[seeded], help="print a fresh random seed and its did:key")
    sub.add_parser("did", parents=[seeded], help="print the did:key for the seed")
    say = sub.add_parser("say", parents=[seeded], help="sign room|nonce|swept-text")
    say.add_argument("room")
    say.add_argument("nonce")
    say.add_argument("text")
    note = sub.add_parser("set", parents=[seeded], help="sign ns|key|nonce|swept-value")
    note.add_argument("ns")
    note.add_argument("key")
    note.add_argument("nonce")
    note.add_argument("value")
    where = sub.add_parser("note", help="print the note path a DID's identity note lives at")
    where.add_argument("did")
    give = sub.add_parser("delegate", parents=[seeded], help="sign a delegation to an agent key")
    give.add_argument("agent", help="the agent's did:key")
    give.add_argument("scope", help="'*', 'r:<room>' or 'kv:<ns>'")
    give.add_argument("days", help="how many days it stays valid")
    give.add_argument("nonce", nargs="?", help="defaults to a millisecond clock")
    audit = sub.add_parser("check", help="verify the delegation lines in a note")
    audit.add_argument("root", help="the root did:key the note belongs to")
    audit.add_argument("file", nargs="?", help="note text; reads stdin when absent")
    args = parser.parse_args()

    if args.cmd == "keygen":
        seed = secrets.token_hex(32)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        print(f"seed: {seed}")
        print(f"did:  {did_of(key)}")
        return

    # Neither of these needs a key: a note path is a hash of a public string, and checking a
    # delegation is the thing anybody can do with no secret at all. They are answered before
    # load_key is ever reached so that they work with no --seed and no $SIGN_SEED.
    if args.cmd == "note":
        print(note_path(args.did))
        return

    if args.cmd == "check":
        body = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        live = check_note(args.root, body)
        print(f"\n{live} live delegation(s)")
        # Nonzero on "nothing usable here", so this is worth putting in a script: a cron job
        # that re-issues before expiry wants an exit code, not prose to grep.
        raise SystemExit(0 if live else 1)

    seed = getattr(args, "seed", None)  # unset when --seed was passed nowhere (SUPPRESS)
    if args.cmd == "did":
        key, _ = load_key(seed)
        print(did_of(key))
        return

    if args.cmd == "delegate":
        if not SCOPE_RE.fullmatch(args.scope):
            raise SystemExit(f"bad scope {args.scope!r}: expected '*', 'r:<room>' or 'kv:<ns>'")
        if not args.days.isdigit() or not 1 <= int(args.days) <= 3650:
            raise SystemExit(f"days must be 1-3650, got {args.days!r}")
        public_key(args.agent)  # refuse to delegate to something that is not a key
        nonce = args.nonce or str(int(time.time() * 1000))
        if not DIGITS_RE.fullmatch(nonce):
            raise SystemExit(f"nonce must be 1-19 ASCII digits, got {nonce!r}")
        expires = str(int(time.time()) + int(args.days) * 86400)
        key, _ = load_key(seed)
        root = did_of(key)
        if root == args.agent:
            raise SystemExit("a key cannot delegate to itself — it already acts for itself")
        sig = signature(key, delegation(root, args.agent, args.scope, expires, nonce))
        print(f"# append to {note_path(root)} — the note of {root}")
        print("# a note is one line: separate this from what is already there with a space")
        print(f"{DELEGATE_TOKEN} {args.agent} {args.scope} {expires} {nonce} {sig}")
        return

    # say/set: build the canonical string over the SWEPT text — what is stored.
    # ASCII digits only, exactly the server's NONCE_RE: str.isdigit() alone also
    # accepts Unicode digits like '١', the script would sign them, and the server
    # would then refuse a signature we told the caller was good (review: PR #54).
    if not re.fullmatch(r"[0-9]{1,19}", args.nonce):
        raise SystemExit(f"nonce must be 1-19 ASCII digits, got {args.nonce!r}")
    if args.cmd == "say":
        canonical = f"{args.room}|{args.nonce}|{swept(args.text, MAX_TEXT_CHARS)}"
    else:
        canonical = f"{args.ns}|{args.key}|{args.nonce}|{swept(args.value, MAX_VALUE_CHARS)}"
    key, _ = load_key(seed)
    print(did_of(key))
    print(signature(key, canonical))


if __name__ == "__main__":
    main()
