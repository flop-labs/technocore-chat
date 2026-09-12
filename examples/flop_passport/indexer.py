"""Bounded HTTP indexer for GitHub contributions and Technocore signed activity.

Network responses are parsed as untrusted JSON. No response value is ever executed.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MAX_RESPONSE = 4 << 20
MAX_EXPORT_RESPONSE = 12 << 20
MAX_GITHUB_PAGES = 20
MAX_INGEST_BODY = 32 << 10
MAX_INGEST_ITEMS = 100


def fetch_json(url: str, token: str = "") -> object:
    headers = {"Accept": "application/json", "User-Agent": "flop-passport-indexer/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=20
    ) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError("source response exceeded 4 MiB")
    return json.loads(body)


def fetch_jsonl(url: str) -> list[object]:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "flop-passport-indexer/0.1"}),
        timeout=20,
    ) as response:
        body = response.read(MAX_EXPORT_RESPONSE + 1)
    if len(body) > MAX_EXPORT_RESPONSE:
        raise ValueError("room export exceeded 12 MiB")
    return [json.loads(line) for line in body.splitlines() if line]


def canonical_technocore_server(server: str) -> str:
    parsed = urllib.parse.urlsplit(server)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Technocore server must be an HTTP(S) base URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Technocore server has an invalid port") from error
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path.rstrip("/"), "", "")
    )


def technocore_state_key(server: str, room: str) -> str:
    return f"technocore:{canonical_technocore_server(server)}:{room}"


def github_pages(url: str, token: str) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_GITHUB_PAGES + 1):
        batch = fetch_json(f"{url}&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("GitHub response must be a list")
        for row in batch:
            if not isinstance(row, dict):
                raise ValueError("GitHub response rows must be objects")
            rows.append(row)
        if len(batch) < 100:
            break
    return rows


def _encoded_ingest(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _post_payload(base: str, token: str, payload: dict) -> object:
    body = _encoded_ingest(payload)
    if len(body) > MAX_INGEST_BODY:
        raise ValueError("ingest request exceeded 32 KiB")
    request = urllib.request.Request(
        base.rstrip("/") + "/api/contributions/ingest",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read(1 << 20))


def _accepted_counts(response: object) -> tuple[int, int]:
    if not isinstance(response, dict):
        raise ValueError("ingest response must be an object")
    accepted, inserted = response.get("accepted"), response.get("inserted")
    if not isinstance(accepted, int) or not isinstance(inserted, int):
        raise ValueError("ingest response counts must be integers")
    return accepted, inserted


def post_batch(base: str, token: str, source: str, cursor: str, items: list[dict]) -> dict:
    batches: list[list[dict]] = []
    current: list[dict] = []
    for item in items:
        candidate = [*current, item]
        if (
            len(candidate) <= MAX_INGEST_ITEMS
            and len(_encoded_ingest({"items": candidate})) <= MAX_INGEST_BODY
        ):
            current = candidate
            continue
        if not current:
            raise ValueError("single contribution exceeds the 32 KiB ingest limit")
        batches.append(current)
        current = [item]
        if len(_encoded_ingest({"items": current})) > MAX_INGEST_BODY:
            raise ValueError("single contribution exceeds the 32 KiB ingest limit")
    if current:
        batches.append(current)

    accepted = inserted = 0
    for batch in batches:
        batch_accepted, batch_inserted = _accepted_counts(
            _post_payload(base, token, {"items": batch})
        )
        if batch_accepted != len(batch):
            raise ValueError("ingest did not accept the complete batch")
        accepted += batch_accepted
        inserted += batch_inserted

    checkpoint = _post_payload(base, token, {"source": source, "cursor": cursor, "items": []})
    _accepted_counts(checkpoint)
    return {"accepted": accepted, "inserted": inserted}


def validated_github_repo(value: object) -> str:
    if (
        not isinstance(value, str)
        or value.count("/") != 1
        or not all(part.replace("-", "").isalnum() for part in value.split("/"))
    ):
        raise ValueError("GitHub repo must be owner/name")
    return value


def github_items(repo: str, identities: dict[str, str], token: str) -> list[dict]:
    repo = validated_github_repo(repo)
    root = f"https://api.github.com/repos/{repo}"
    metadata = fetch_json(root, token)
    try:
        canonical_repo = validated_github_repo(
            metadata.get("full_name") if isinstance(metadata, dict) else None
        )
    except ValueError as error:
        raise ValueError("GitHub repository metadata needs canonical full_name") from error

    root = f"https://api.github.com/repos/{canonical_repo}"
    pulls = github_pages(
        root + "/pulls?state=closed&sort=updated&direction=desc&per_page=100", token
    )
    issues = github_pages(
        root + "/issues?state=all&sort=updated&direction=desc&per_page=100", token
    )
    items = []
    for row in pulls:
        login = row.get("user", {}).get("login") if isinstance(row, dict) else None
        did = identities.get(login or "")
        if not did or not row.get("merged_at"):
            continue
        items.append(
            {
                "did": did,
                "source": "github",
                "source_id": f"{canonical_repo}:pr:{row['number']}",
                "kind": "pull_request",
                "title": str(row.get("title", ""))[:300],
                "url": row.get("html_url"),
                "occurred_at": calendar.timegm(
                    time.strptime(row["merged_at"], "%Y-%m-%dT%H:%M:%SZ")
                ),
                "evidence": {
                    "repo": canonical_repo,
                    "number": row["number"],
                    "state": "merged",
                    "api_url": row.get("url"),
                },
            }
        )
    for row in issues:
        if not isinstance(row, dict) or "pull_request" in row:
            continue
        login = row.get("user", {}).get("login")
        did = identities.get(login or "")
        if not did:
            continue
        items.append(
            {
                "did": did,
                "source": "github",
                "source_id": f"{canonical_repo}:issue:{row['number']}",
                "kind": "issue",
                "title": str(row.get("title", ""))[:300],
                "url": row.get("html_url"),
                "occurred_at": calendar.timegm(
                    time.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                ),
                "evidence": {
                    "repo": canonical_repo,
                    "number": row["number"],
                    "state": row.get("state"),
                    "api_url": row.get("url"),
                },
            }
        )
    return items


def technocore_items(server: str, room: str, since: int) -> tuple[list[dict], int]:
    server = canonical_technocore_server(server)
    safe_room = urllib.parse.quote(room, safe="")
    url = f"{server}/r/{safe_room}?format=json&since={since}&limit=200"
    payload = fetch_json(url)
    records = payload.get("messages", payload) if isinstance(payload, dict) else payload
    first_seq = payload.get("first_seq") if isinstance(payload, dict) else None
    if isinstance(first_seq, int) and first_seq > since + 1:
        records = fetch_jsonl(f"{server}/r/{safe_room}/export")
    items, cursor = [], since
    for row in records if isinstance(records, list) else []:
        if not isinstance(row, dict):
            continue
        seq, nonce = row.get("seq"), row.get("nonce")
        if not isinstance(seq, int):
            continue
        cursor = max(cursor, seq)
        if (
            seq <= since
            or not isinstance(nonce, int)
            or not str(row.get("from", "")).startswith("did:key:")
        ):
            continue
        stamp = row.get("ts")
        if not isinstance(stamp, str):
            continue
        try:
            occurred_at = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
        except (AttributeError, TypeError, ValueError):
            continue
        items.append(
            {
                "did": row["from"],
                "source": "technocore",
                "source_id": f"{server}:{room}:{seq}",
                "kind": "signed_post",
                "title": str(row.get("text", ""))[:300],
                "url": f"{server}/humans#r/{urllib.parse.quote(room)}/{seq}",
                "occurred_at": occurred_at,
                "evidence": {"server": server, "room": room, "seq": seq, "nonce": nonce},
            }
        )
    return items, cursor


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Index FLOP Passport contribution evidence")
    parser.add_argument("--passport", default="http://127.0.0.1:8090")
    parser.add_argument(
        "--identity-map", type=Path, required=True, help="JSON map of GitHub login to DID"
    )
    parser.add_argument("--github-repo", action="append", default=[])
    parser.add_argument("--technocore", default="https://technocore.chat")
    parser.add_argument("--room", action="append", default=[])
    parser.add_argument("--state", type=Path, default=Path(".passport-indexer.json"))
    args = parser.parse_args()
    token = os.environ.get("FLOP_PASSPORT_INDEXER_TOKEN", "")
    if not token:
        raise SystemExit("FLOP_PASSPORT_INDEXER_TOKEN is required")
    identities, state = load_json(args.identity_map, {}), load_json(args.state, {})
    github_token = os.environ.get("GITHUB_TOKEN", "")
    for repo in args.github_repo:
        items = github_items(repo, identities, github_token)
        post_batch(args.passport, token, "github", str(int(time.time())), items)
    technocore = canonical_technocore_server(args.technocore)
    for room in args.room:
        key = technocore_state_key(technocore, room)
        items, cursor = technocore_items(technocore, room, int(state.get(key, 0)))
        post_batch(args.passport, token, "technocore", str(cursor), items)
        state[key] = cursor
    save_state(args.state, state)


if __name__ == "__main__":
    main()
