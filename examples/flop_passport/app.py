"""Local FLOP Passport example: DID-owned profiles and verified contribution evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

HERE = Path(__file__).parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))
import didkey  # noqa: E402

DATA_ROOT = Path(os.environ.get("FLOP_PASSPORT_ROOT", HERE / ".data"))
DB_PATH = DATA_ROOT / "passport.sqlite3"
INDEXER_TOKEN = os.environ.get("FLOP_PASSPORT_INDEXER_TOKEN", "")
CHALLENGE_TTL = 300
MAX_BODY = 32 << 10
USERNAME = re.compile(r"[a-z0-9][a-z0-9_-]{2,31}")
GITHUB = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
 did TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, avatar_url TEXT, bio TEXT NOT NULL,
 github TEXT, x_handle TEXT, kind TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_github ON profiles(github);
CREATE TABLE IF NOT EXISTS challenges (
 nonce_hash TEXT PRIMARY KEY, did TEXT NOT NULL, profile_digest TEXT NOT NULL,
 requester_hash TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_challenges_rate ON challenges(requester_hash, created_at);
CREATE TABLE IF NOT EXISTS contributions (
 id TEXT PRIMARY KEY, did TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT, evidence TEXT NOT NULL,
 occurred_at INTEGER NOT NULL, ingested_at INTEGER NOT NULL, UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_contributions_did_time ON contributions(did, occurred_at DESC);
CREATE TABLE IF NOT EXISTS indexer_state (
 source TEXT PRIMARY KEY, cursor TEXT, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, actor_did TEXT,
 requester_hash TEXT, outcome TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""


@contextmanager
def database():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _audit(db, event: str, outcome: str, did: str | None, requester: str | None):
    db.execute(
        "INSERT INTO audit_log(event,actor_did,requester_hash,outcome,created_at) VALUES(?,?,?,?,?)",
        (event, did, requester, outcome, int(time.time())),
    )
    db.execute(
        "DELETE FROM audit_log WHERE id <= COALESCE((SELECT MAX(id) - 10000 FROM audit_log), 0)"
    )


def _client_hash(request: Request) -> str:
    return _hash(request.client.host if request.client else "local")


async def _payload(request: Request) -> dict:
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
    except ValueError as error:
        raise ValueError("content-length must be an integer") from error
    if declared > MAX_BODY:
        raise ValueError("body is too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValueError("body is too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _clean_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("profile must be an object")

    def field(name, size):
        item = value.get(name, "")
        if not isinstance(item, str):
            raise ValueError(f"{name} must be text")
        return item.strip()[:size]

    username = field("username", 32).lower()
    github = field("github", 39).removeprefix("@")
    x_handle = field("x_handle", 15).removeprefix("@")
    avatar_url = field("avatar_url", 500)
    kind = value.get("kind", "human")
    if not USERNAME.fullmatch(username):
        raise ValueError("username must be 3-32 lowercase letters, digits, _ or -")
    if github and not GITHUB.fullmatch(github):
        raise ValueError("github is not a valid GitHub login")
    if x_handle and not X_HANDLE.fullmatch(x_handle):
        raise ValueError("x_handle is not valid")
    if avatar_url and not avatar_url.startswith("https://"):
        raise ValueError("avatar_url must use https")
    if kind not in ("human", "agent"):
        raise ValueError("kind must be human or agent")
    return {
        "username": username,
        "avatar_url": avatar_url or None,
        "bio": field("bio", 280),
        "github": github or None,
        "x_handle": x_handle or None,
        "kind": kind,
    }


def _profile_digest(profile: dict) -> str:
    canonical = json.dumps(profile, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _hash(canonical)


def challenge_message(did: str, nonce: str, expires_at: int, profile: dict) -> str:
    return "\n".join(
        (
            "FLOP Passport profile ownership",
            f"DID: {did}",
            f"Nonce: {nonce}",
            f"Expires: {expires_at}",
            f"Profile-SHA256: {_profile_digest(profile)}",
        )
    )


async def create_challenge(request: Request):
    try:
        payload = await _payload(request)
        did = payload.get("did", "")
        didkey.public_key(did)
        profile = _clean_profile(payload.get("profile"))
    except (ValueError, json.JSONDecodeError, didkey.DidError) as error:
        return JSONResponse({"error": str(error)}, 400)
    requester = _client_hash(request)
    now = int(time.time())
    with database() as db:
        db.execute("DELETE FROM challenges WHERE expires_at<=?", (now,))
        count = db.execute(
            "SELECT COUNT(*) FROM challenges WHERE requester_hash=? AND created_at>?",
            (requester, now - 60),
        ).fetchone()[0]
        if count >= 5:
            _audit(db, "challenge.create", "rate_limited", did, requester)
            return JSONResponse({"error": "too many challenges; retry in one minute"}, 429)
        nonce = secrets.token_urlsafe(24)
        expires_at = now + CHALLENGE_TTL
        message = challenge_message(did, nonce, expires_at, profile)
        db.execute(
            "INSERT INTO challenges VALUES(?,?,?,?,?,?,NULL)",
            (_hash(nonce), did, _profile_digest(profile), requester, now, expires_at),
        )
        _audit(db, "challenge.create", "success", did, requester)
    return JSONResponse({"nonce": nonce, "message": message, "expires_at": expires_at})


async def verify_and_save(request: Request):
    try:
        payload = await _payload(request)
        did, nonce, signature = (payload.get(name, "") for name in ("did", "nonce", "signature"))
        if not all(isinstance(item, str) and item for item in (did, nonce, signature)):
            raise ValueError("did, nonce and signature are required")
        profile = _clean_profile(payload.get("profile"))
    except (ValueError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, 400)
    requester, now = _client_hash(request), int(time.time())
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM challenges WHERE nonce_hash=? AND did=?", (_hash(nonce), did)
        ).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] <= now:
            _audit(db, "challenge.verify", "expired_or_replayed", did, requester)
            return JSONResponse({"error": "challenge expired, used, or invalid"}, 401)
        profile_digest = _profile_digest(profile)
        expected = challenge_message(did, nonce, row["expires_at"], profile)
        if not secrets.compare_digest(row["profile_digest"], profile_digest):
            _audit(db, "challenge.verify", "profile_changed", did, requester)
            return JSONResponse({"error": "profile does not match the signed challenge"}, 401)
        try:
            didkey.verify(did, signature, expected)
        except (didkey.DidError, didkey.SignatureError) as error:
            _audit(db, "challenge.verify", "bad_signature", did, requester)
            return JSONResponse({"error": str(error)}, 403)
        conflict = db.execute(
            "SELECT 1 FROM profiles WHERE username=? AND did<>?", (profile["username"], did)
        ).fetchone()
        if conflict:
            _audit(db, "profile.update", "username_taken", did, requester)
            return JSONResponse({"error": "username is already claimed"}, 409)
        changed = db.execute(
            "UPDATE challenges SET used_at=? WHERE nonce_hash=? AND used_at IS NULL",
            (now, _hash(nonce)),
        ).rowcount
        if changed != 1:
            _audit(db, "challenge.verify", "replay_race", did, requester)
            return JSONResponse({"error": "challenge already used"}, 401)
        db.execute(
            """INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(did) DO UPDATE SET username=excluded.username,
            avatar_url=excluded.avatar_url,bio=excluded.bio,github=excluded.github,
            x_handle=excluded.x_handle,kind=excluded.kind,updated_at=excluded.updated_at""",
            (
                did,
                profile["username"],
                profile["avatar_url"],
                profile["bio"],
                profile["github"],
                profile["x_handle"],
                profile["kind"],
                now,
            ),
        )
        _audit(db, "profile.update", "success", did, requester)
    return JSONResponse({"verified": True, "did": did, "username": profile["username"]})


def _badges(counts: dict) -> list[dict]:
    rules = (
        (
            "early-builder",
            "Early builder",
            counts["total"] >= 1,
            "At least 1 verified contribution",
        ),
        ("shipper", "Shipper", counts["merged_prs"] >= 3, "At least 3 merged PRs"),
        ("bug-hunter", "Bug hunter", counts["issues"] >= 3, "At least 3 verified issues"),
        (
            "field-researcher",
            "Field researcher",
            counts["technocore"] >= 5,
            "At least 5 signed Technocore posts",
        ),
    )
    return [{"id": i, "label": label, "rule": rule} for i, label, earned, rule in rules if earned]


def _public_profile(db, identifier: str) -> dict | None:
    profile = db.execute(
        "SELECT * FROM profiles WHERE did=? OR username=? LIMIT 1",
        (identifier, identifier.lower()),
    ).fetchone()
    if not profile:
        return None
    activity = db.execute(
        "SELECT id,source,kind,title,url,evidence,occurred_at FROM contributions WHERE did=? "
        "ORDER BY occurred_at DESC LIMIT 100",
        (profile["did"],),
    ).fetchall()
    totals = db.execute(
        """SELECT COUNT(*) AS total,
        SUM(source='github' AND kind='pull_request'
            AND json_extract(evidence, '$.state')='merged') AS merged_prs,
        SUM(source='github' AND kind='issue') AS issues,
        SUM(source='technocore') AS technocore
        FROM contributions WHERE did=?""",
        (profile["did"],),
    ).fetchone()
    declared = {
        key: profile[key] for key in ("username", "avatar_url", "bio", "github", "x_handle", "kind")
    }
    verified = [dict(row) | {"evidence": json.loads(row["evidence"])} for row in activity]
    counts = {key: int(totals[key] or 0) for key in ("total", "merged_prs", "issues", "technocore")}
    return {
        "did": profile["did"],
        "self_declared": declared,
        "verified": {"counts": counts, "badges": _badges(counts), "contributions": verified},
    }


async def profile(request: Request):
    identifier = request.path_params["identifier"]
    with database() as db:
        result = _public_profile(db, identifier)
    return JSONResponse(result or {"error": "profile not found"}, 200 if result else 404)


async def search(request: Request):
    query = request.query_params.get("q", "").strip()[:200]
    if not query:
        with database() as db:
            rows = db.execute(
                "SELECT did,username,avatar_url,bio,github,kind FROM profiles "
                "ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        return JSONResponse({"results": [dict(row) for row in rows]})
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with database() as db:
        rows = db.execute(
            "SELECT did,username,avatar_url,bio,github,kind FROM profiles "
            "WHERE did=? OR username LIKE ? ESCAPE '\\' OR github LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT 20",
            (query, f"%{escaped.lower()}%", f"%{escaped.removeprefix('@')}%"),
        ).fetchall()
    return JSONResponse({"results": [dict(row) for row in rows]})


def _validated_contribution(raw: object) -> tuple:
    if not isinstance(raw, dict):
        raise ValueError("each contribution must be an object")
    did, source = raw.get("did"), raw.get("source")
    if not isinstance(did, str):
        raise ValueError("did must be text")
    didkey.public_key(did)
    if source not in ("github", "technocore"):
        raise ValueError("source must be github or technocore")
    source_id, kind, title = (raw.get(name) for name in ("source_id", "kind", "title"))
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id is required")
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind is required")
    if not isinstance(title, str) or not title:
        raise ValueError("source_id, kind and title are required")
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    if source == "github" and not all(key in evidence for key in ("repo", "number", "state")):
        raise ValueError("GitHub evidence needs repo, number and state")
    if source == "technocore":
        try:
            if "signature" in evidence:
                canonical = f"{evidence['room']}|{evidence['nonce']}|{evidence['text']}"
                signature = evidence["signature"]
                if not isinstance(signature, str):
                    raise ValueError
                didkey.verify(did, signature, canonical)
            elif not all(key in evidence for key in ("server", "room", "seq", "nonce")):
                raise ValueError
        except (KeyError, TypeError, ValueError, didkey.DidError, didkey.SignatureError) as error:
            raise ValueError(
                "Technocore evidence needs a signature or server-attested record"
            ) from error
    occurred = raw.get("occurred_at")
    if not isinstance(occurred, int) or occurred < 0:
        raise ValueError("occurred_at must be a non-negative integer")
    url = raw.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise ValueError("url must use https")
    return (
        f"{source}:{source_id}"[:240],
        did,
        source,
        source_id[:180],
        kind[:40],
        title[:300],
        url,
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        occurred,
        int(time.time()),
    )


async def ingest(request: Request):
    supplied = request.headers.get("authorization", "")
    if not INDEXER_TOKEN or not secrets.compare_digest(supplied, f"Bearer {INDEXER_TOKEN}"):
        return JSONResponse({"error": "not found"}, 404)
    try:
        payload = await _payload(request)
        items = payload.get("items", [])
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("items must be a list of at most 100")
        validated = [_validated_contribution(item) for item in items]
        source, cursor = payload.get("source"), payload.get("cursor")
        if source is not None and source not in ("github", "technocore"):
            raise ValueError("source must be github or technocore")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be text")
    except (ValueError, json.JSONDecodeError, didkey.DidError) as error:
        return JSONResponse({"error": str(error)}, 400)
    inserted = updated = 0
    with database() as db:
        for row in validated:
            existed = db.execute(
                "SELECT 1 FROM contributions WHERE source=? AND source_id=?", (row[2], row[3])
            ).fetchone()
            changed = db.execute(
                """INSERT INTO contributions VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                did=excluded.did,kind=excluded.kind,title=excluded.title,url=excluded.url,
                evidence=excluded.evidence,occurred_at=excluded.occurred_at,
                ingested_at=excluded.ingested_at
                WHERE contributions.source='github' AND
                (contributions.did IS NOT excluded.did OR
                 contributions.kind IS NOT excluded.kind OR
                 contributions.title IS NOT excluded.title OR
                 contributions.url IS NOT excluded.url OR
                 contributions.evidence IS NOT excluded.evidence OR
                 contributions.occurred_at IS NOT excluded.occurred_at)""",
                row,
            ).rowcount
            if existed:
                updated += changed
            else:
                inserted += changed
        if source:
            db.execute(
                "INSERT INTO indexer_state VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "cursor=excluded.cursor,updated_at=excluded.updated_at",
                (source, (cursor or "")[:500], int(time.time())),
            )
        _audit(
            db,
            "contributions.ingest",
            f"inserted:{inserted},updated:{updated}",
            None,
            _client_hash(request),
        )
    return JSONResponse({"accepted": len(validated), "inserted": inserted, "updated": updated})


def _csp(html: str) -> str:
    blocks = re.findall(r"<(script|style)\b[^>]*>(.*?)</\1>", html, re.DOTALL)
    sources = {
        tag: base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        for tag, body in blocks
    }
    return (
        "default-src 'none'; connect-src 'self'; img-src 'self' data: https:; "
        f"script-src 'sha256-{sources['script']}'; style-src 'sha256-{sources['style']}'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


HTML = (HERE / "index.html").read_text(encoding="utf-8")
CSP = _csp(HTML)


async def passport_page(_request: Request):
    return Response(
        HTML,
        media_type="text/html",
        headers={
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


app = Starlette(
    routes=[
        Route("/", passport_page),
        Route("/u/{identifier:path}", passport_page),
        Route("/api/challenges", create_challenge, methods=["POST"]),
        Route("/api/verify", verify_and_save, methods=["POST"]),
        Route("/api/search", search),
        Route("/api/profiles/{identifier:path}", profile),
        Route("/api/contributions/ingest", ingest, methods=["POST"]),
    ]
)
